"""The MCP <-> Anthropic bridge, rebuilt for routing to many workers.

This is the one file that genuinely differs from ResearchMesh's copy, and it is
the whole reason this repo exists. Three changes, each fixing something that
makes a fleet of identical workers impossible:

1. **Tool names are namespaced.** ResearchMesh passes `t.name` through verbatim.
   That is fine with one server; with three ResearchMesh workers it sends three
   tools all called `delegate` and the API rejects the request outright:

       400 invalid_request_error: tools: Tool names must be unique.

   Every tool is therefore declared as `<worker>__<tool>` and the prefix is
   stripped again before the call goes out over the wire. The worker never
   learns it was renamed.

2. **Worker identity is injected into the description.** Namespacing alone gets
   you `gpu__delegate` and `winbox__delegate` carrying *byte-identical* text —
   `mcp_server.py` hardcodes one `_DELEGATE_DESCRIPTION` constant, so every
   worker in the fleet describes itself the same way and the model has nothing
   to route on. The `description` field of a `[mcp].servers` entry is prepended
   as a `[worker: name] ...` header. Config-side rather than worker-side on
   purpose: it needs no coordinated redeploy, and it works against MCP servers
   that aren't ResearchMesh at all.

3. **Execution fans out.** ResearchMesh runs tool_use blocks in a plain `for`
   loop. Three four-minute delegations there take twelve minutes; here they
   take four. Blocks are grouped by owning worker and the *groups* run
   concurrently — sequential within a group, because a ResearchMesh worker
   holds one mouse, one browser page and one IPython kernel behind its own
   `asyncio.Lock`. Firing two calls at one worker would not make it faster; it
   would just park the second on that lock until it timed out.

One structural change falls out of this: the tool index is built once per user
turn by `Chat` and handed to `execute_blocks`, instead of every execution pass
re-deriving owners with a fresh `list_tools` round trip per client. Over a LAN
that was invisible. Against workers on other machines it is a round trip per
worker per iteration, up to MAX_TOOL_ITERATIONS times a turn.
"""

import asyncio
import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any, Literal, Optional, Protocol

from anthropic.types import ToolResultBlockParam
from mcp.types import CallToolResult, ImageContent, TextContent, Tool


class Worker(Protocol):
    """The two methods this bridge actually needs from a worker.

    Structural rather than `MCPClient` on purpose. Nothing here depends on the
    transport, so a protocol both states that honestly and lets smoke_test.py
    exercise the namespacing and fan-out against fakes without a live fleet —
    which is the difference between those invariants being tested and merely
    being asserted in a comment.

    Note `Mapping` rather than `dict` wherever a fleet is passed around: dict is
    invariant in its value type, so `dict[str, MCPClient]` would not satisfy
    `dict[str, Worker]` even though every MCPClient is one.
    """

    async def list_tools(self) -> list[Tool]: ...

    async def call_tool(
        self, tool_name: str, tool_input
    ) -> CallToolResult | None: ...


# Separator between the worker prefix and the worker's own tool name. Double
# underscore matches what Claude Code uses for the same job (`mcp__server__tool`),
# so the shape is already familiar to the model, and a single underscore would
# be ambiguous against the many tool names that contain one.
SEPARATOR = "__"

# The Anthropic API constrains tool names to ^[a-zA-Z0-9_-]{1,128}$. A worker
# named "gpu-box" is fine; one named "gpu box" or "n8n.local" is not, and would
# 400 the whole request rather than just that tool.
_ILLEGAL = re.compile(r"[^a-zA-Z0-9_-]")
_MAX_NAME_LEN = 128


def _digest(text: str) -> str:
    """Four stable hex chars, for disambiguating a truncated name.

    Deliberately deterministic: prompt caching keys on the rendered prefix, and
    tools render before the system prompt. A name that changed between requests
    would silently cost the whole cache hit every turn.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:4]


def _legalise(name: str, used: set[str]) -> str:
    """Coerce `name` into a unique, API-legal tool name."""
    clean = _ILLEGAL.sub("_", name) or "tool"

    if len(clean) > _MAX_NAME_LEN:
        keep = _MAX_NAME_LEN - 5  # room for "_" + 4 hex chars
        clean = f"{clean[:keep]}_{_digest(name)}"

    candidate, counter = clean, 2
    while candidate in used:
        suffix = f"_{counter}"
        candidate = f"{clean[: _MAX_NAME_LEN - len(suffix)]}{suffix}"
        counter += 1

    used.add(candidate)
    return candidate


class ToolIndex:
    """One turn's view of the fleet: what to declare, and who owns what.

    Built once per user turn. Holds the Anthropic-shaped schemas to send, and
    the mapping needed to undo the namespacing when a call comes back.
    """

    def __init__(self) -> None:
        self.tool_defs: list[dict[str, Any]] = []
        # namespaced name -> (owning client, the worker's own name for it)
        self._owners: dict[str, tuple[Worker, str]] = {}
        # namespaced name -> worker id, for log lines and error messages
        self._workers: dict[str, str] = {}

    def add(
        self,
        declared_name: str,
        client: Worker,
        worker_id: str,
        bare_name: str,
        schema: dict[str, Any],
    ) -> None:
        self._owners[declared_name] = (client, bare_name)
        self._workers[declared_name] = worker_id
        self.tool_defs.append(schema)

    def resolve(self, declared_name: str) -> Optional[tuple[Worker, str]]:
        return self._owners.get(declared_name)

    def worker_of(self, declared_name: str) -> str:
        return self._workers.get(declared_name, "?")

    def __len__(self) -> int:
        return len(self.tool_defs)

    def worker_ids(self) -> list[str]:
        """Workers that answered `list_tools` this turn, in declaration order.

        This is the fleet as it exists *right now*, which is what `/workers`
        reports and what `/dagent <name>` resolves against. A worker that failed
        to connect never reached this index, and one that died since startup was
        dropped by `build`; either way it is not here, and both are the same
        thing from the model's point of view — a machine it cannot reach.
        """
        seen: dict[str, None] = {}
        for worker_id in self._workers.values():
            seen.setdefault(worker_id, None)
        return list(seen)

    def defs_for(self, worker_id: str) -> list[dict[str, Any]]:
        """Just this worker's tool schemas — the `/dagent <name>` pin."""
        names = {
            declared
            for declared, owner in self._workers.items()
            if owner == worker_id
        }
        return [t for t in self.tool_defs if t["name"] in names]

    def summary(self) -> str:
        """`2 workers, 5 tools` — printed at startup and by /workers."""
        workers = len(set(self._workers.values()))
        tools = len(self.tool_defs)
        return (
            f"{workers} worker{'s' if workers != 1 else ''}, "
            f"{tools} tool{'s' if tools != 1 else ''}"
        )


class ToolManager:
    @classmethod
    async def build(
        cls,
        clients: Mapping[str, Worker],
        descriptions: Optional[Mapping[str, str]] = None,
        reserved: Optional[set[str]] = None,
    ) -> ToolIndex:
        """List every worker's tools and namespace them into one flat set.

        `descriptions` maps a worker id to the routing blurb from its
        config.toml entry — what that box *is*, which is the thing the model
        needs and the worker itself cannot tell it.

        `reserved` is names already spoken for elsewhere in the request — in
        practice the router's own local tools, which are declared unprefixed
        alongside this index. Uniqueness is enforced across the whole `tools`
        array, not per source, so a name this builder cannot see is still a name
        it must not emit. A collision is unlikely (a namespaced tool contains
        `__` and a local one does not), but the cost of one is a 400 that takes
        down the entire request rather than the single tool.

        A worker that fails to answer `list_tools` is skipped with a warning
        rather than taking the turn down; it may have died since startup, and
        the rest of the fleet is still usable. That matches how main.py treats a
        worker that fails to connect in the first place.
        """
        index = ToolIndex()
        used: set[str] = set(reserved or ())
        descriptions = descriptions or {}

        for worker_id, client in clients.items():
            try:
                tool_models = await client.list_tools()
            except Exception as e:
                print(f"[router] {worker_id}: list_tools failed, skipping — {e}")
                continue

            prefix = _legalise(worker_id, set())
            blurb = descriptions.get(worker_id, "").strip()

            for tool in tool_models:
                declared = _legalise(f"{prefix}{SEPARATOR}{tool.name}", used)

                # The header goes first so it survives any downstream
                # truncation, and names the worker in the same breath as what
                # the worker is for.
                header = f"[worker: {worker_id}]"
                if blurb:
                    header = f"{header} {blurb}"
                description = f"{header}\n\n{tool.description or ''}".strip()

                index.add(
                    declared,
                    client,
                    worker_id,
                    tool.name,
                    {
                        "name": declared,
                        "description": description,
                        # mcp 2.0 renamed the model fields to snake_case
                        # (`inputSchema` -> `input_schema`, `isError` ->
                        # `is_error`, `mimeType` -> `mime_type` below). The
                        # camelCase spellings survive as serialization aliases,
                        # so constructing still works either way — but attribute
                        # *reads* like this one do not, and fail at runtime
                        # rather than at import.
                        "input_schema": tool.input_schema,
                    },
                )

        return index

    @classmethod
    def _build_tool_result_part(
        cls,
        tool_use_id: str,
        text: str,
        status: Literal["success", "error"],
    ) -> ToolResultBlockParam:
        """Builds a tool result part dictionary."""
        return {
            "tool_use_id": tool_use_id,
            "type": "tool_result",
            "content": text,
            "is_error": status == "error",
        }

    @classmethod
    def _build_tool_result_part_content(
        cls,
        tool_use_id: str,
        content,
        status: Literal["success", "error"],
    ) -> ToolResultBlockParam:
        """Like _build_tool_result_part, but accepts pre-built content
        (a string, or a list of content blocks e.g. text + image)."""
        return {
            "tool_use_id": tool_use_id,
            "type": "tool_result",
            "content": content,
            "is_error": status == "error",
        }

    @classmethod
    async def _call_one(
        cls, index: ToolIndex, tool_request
    ) -> ToolResultBlockParam:
        """Execute a single tool_use block against its owning worker."""
        tool_use_id = tool_request.id
        declared_name = tool_request.name
        tool_input = tool_request.input

        owner = index.resolve(declared_name)
        if owner is None:
            return cls._build_tool_result_part(
                tool_use_id, "Could not find that tool", "error"
            )
        client, bare_name = owner

        try:
            tool_output: CallToolResult | None = await client.call_tool(
                bare_name, tool_input
            )
            items = tool_output.content if tool_output else []
            text_list = [
                item.text for item in items if isinstance(item, TextContent)
            ]
            image_items = [
                item for item in items if isinstance(item, ImageContent)
            ]
            content_json = json.dumps(text_list)
            status: Literal["success", "error"] = (
                "error" if tool_output and tool_output.is_error else "success"
            )

            if image_items:
                # Forward images as real image content blocks rather than
                # silently dropping them and keeping only the text. A
                # ResearchMesh worker returns one from every `computer`
                # screenshot, so on a GUI delegation this is most of the value.
                content = [{"type": "text", "text": content_json}] + [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": item.mime_type,
                            "data": item.data,
                        },
                    }
                    for item in image_items
                ]
                return cls._build_tool_result_part_content(
                    tool_use_id, content, status
                )

            return cls._build_tool_result_part(
                tool_use_id, content_json, status
            )
        except Exception as e:
            worker = index.worker_of(declared_name)
            error_message = (
                f"Error executing '{bare_name}' on worker '{worker}': {e}"
            )
            print(f"[router] {error_message}")
            return cls._build_tool_result_part(
                tool_use_id, json.dumps({"error": error_message}), "error"
            )

    @classmethod
    async def execute_blocks(
        cls,
        index: ToolIndex,
        tool_use_blocks,
        max_parallel: int = 8,
    ) -> list[ToolResultBlockParam]:
        """Run every tool_use block, fanning out across workers.

        Grouped by owning worker: groups run concurrently, blocks within a group
        run in order. That mirrors the constraint on the other end — a
        ResearchMesh worker serialises `delegate` behind an `asyncio.Lock`
        because it has one mouse, one browser page and one kernel — so issuing
        two calls at once to the same worker buys nothing and risks the second
        aging out on that lock while it waits.

        `max_parallel` caps how many workers are busy at once, bounding both
        simultaneous API spend downstream and how badly the logs interleave.

        Results come back in the original block order. The API does not require
        that, but a tool_result list that matches the tool_use list makes a
        transcript readable, and every block is guaranteed exactly one result —
        an unanswered tool_use poisons every later request in the session.
        """
        if not tool_use_blocks:
            return []

        groups: dict[str, list] = {}
        for block in tool_use_blocks:
            groups.setdefault(index.worker_of(block.name), []).append(block)

        semaphore = asyncio.Semaphore(max(1, max_parallel))

        async def run_group(
            blocks: list,
        ) -> list[tuple[str, ToolResultBlockParam]]:
            out: list[tuple[str, ToolResultBlockParam]] = []
            async with semaphore:
                for block in blocks:
                    out.append((block.id, await cls._call_one(index, block)))
            return out

        if len(groups) > 1:
            names = ", ".join(f"{w}({len(b)})" for w, b in groups.items())
            print(f"[router] fanning out to {len(groups)} workers: {names}")

        # return_exceptions=True so one worker blowing up in a way _call_one
        # did not already catch cannot orphan the *other* workers' blocks.
        finished = await asyncio.gather(
            *(run_group(blocks) for blocks in groups.values()),
            return_exceptions=True,
        )

        by_id: dict[str, ToolResultBlockParam] = {}
        for group_blocks, outcome in zip(groups.values(), finished):
            if isinstance(outcome, BaseException):
                print(f"[router] worker group failed: {outcome}")
                for block in group_blocks:
                    by_id[block.id] = cls._build_tool_result_part(
                        block.id, f"Worker group failed: {outcome}", "error"
                    )
                continue
            for block_id, result in outcome:
                by_id[block_id] = result

        return [by_id[block.id] for block in tool_use_blocks]
