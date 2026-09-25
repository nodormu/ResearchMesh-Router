import os
from collections.abc import Mapping
from pathlib import Path

from anthropic.types import MessageParam, ToolResultBlockParam
from mcp.types import TextContent

from core import local_tools
from core.claude import Claude
from core.claude_learned_schemas import SHELL_EXECUTABLE
from core.tools import ToolIndex, ToolManager, Worker

# Human-facing name of the interpreter the LOCAL `bash` tool actually runs
# commands through (e.g. "bash", "zsh") — resolved once at import time from
# SHELL_EXECUTABLE (core/claude_learned_schemas.py, copied verbatim from
# ResearchMesh, including why that constant isn't named BASH_SHELL).
# Interpolated into SYSTEM_PROMPT below. Deliberately not importing SH_TARGET
# here the way ResearchMesh's own chat.py does: that fact is only relevant to
# writing a standalone #!/bin/sh script, which is a poor match for how this
# router's own bash gets used (quick local bookkeeping, not primary work —
# see SYSTEM_PROMPT's own guidance below), so it isn't worth the extra
# prompt real estate here.
_SHELL_EXECUTABLE_NAME = Path(SHELL_EXECUTABLE).name

# Raised 30 -> 75 -> 200 across this project's history (30->75 happened
# on the original Linux ResearchMesh client, ported here — see that
# repo's researchmesh_client_dev_log.md for the incident: a single turn
# doing iterative debugging against a buggy third-party MCP server
# chewed through 20+ iterations just fixing/working around that
# server's own bugs). 200 is a safety-valve headroom increase, not a
# response to a specific new incident — it exists so a long, genuinely
# productive turn doesn't get cut off mid-task purely on iteration
# count. Counts rounds of the chat loop (each of which can batch
# several tool_use calls in one response), not a literal per-tool-call
# counter, and resets every new user message, never across a whole
# conversation.
MAX_TOOL_ITERATIONS = 200

# Separate, small grace budget for continuations the API contract makes
# mandatory (an open pause_turn; a server_tool_use left dangling by a mixed
# tool_use response) — MAX_TOOL_ITERATIONS alone must never block these.
# 5 matches Anthropic's own reference example's `max_continuations` default.
# See Chat._finalize_turn for what happens if even this runs out. Ported
# from ResearchMesh core/chat.py (commit 672aae1) — same bug class is
# reachable here too since this router also exposes web_search/web_fetch
# as real Anthropic server tools (core/claude_learned_schemas.py).
EXTRA_CONTINUATION_LIMIT = 5

# Set CLAUDE_SHOW_USAGE=1 to print token and cache counters per request. Prompt
# caching fails *silently* (a too-short prefix or a changed byte early in the
# prefix just means no hit, with no error), so this is the only way to confirm
# the cache_control breakpoint in core/claude.py is actually paying off.
SHOW_USAGE = os.getenv("CLAUDE_SHOW_USAGE") == "1"

# Sent as the `system` parameter on every request.
#
# This prompt carries two facts at once, and both are things Claude cannot infer
# from the schemas. First, the tool list is split across machines: some tools run
# *here* and the rest run on workers elsewhere, and nothing in a tool definition
# says which. Second, the workers are separate computers — left to the schemas
# alone the model treats the whole fleet as one, reading a path from one worker
# and writing it on another.
#
# Before local tools were added this prompt could open with the much stronger
# "you have no tools of your own". That sentence is now false, and the split
# below replaces it: the namespacing is what tells local from remote apart.
SYSTEM_PROMPT = f"""\
You are the router in a command-line client. You have two kinds of tools, and
telling them apart is the first thing to get right on every call.

LOCAL tools have plain names — `bash`, `python`, `computer`, `browser_navigate`,
`memory`, `sql_query`, and so on. They run on THIS machine, the one the router
itself is running on. They are fast, they return immediately, and their effects
land here.

REMOTE tools are named `<worker>__<tool>`. The part before the double underscore
is the worker the call runs on, and the description opens with `[worker: name]`
followed by what that machine is for. Read that header before choosing — it is the
only thing distinguishing two workers that expose identically-named tools.

There is no sandbox or code-execution container behind the local tools above, and
no `code_execution`, `bash_code_execution`, or `text_editor_code_execution`
definitions exist among them, whatever training data makes that feel like a gap —
`bash` and `python` here run as the real user on this real machine, with real
filesystem and network access, not inside anything separate. The 2026
`web_search`/`web_fetch` variants filter results using server-side code execution
internally, which is likely where that instinct comes from, but that machinery
lives inside those two tools, not as something callable on its own. The same
absence holds for a worker unless its own tool list says otherwise — check what it
actually declares rather than assuming a client like this one typically ships one.

The LOCAL `bash` runs commands through **{_SHELL_EXECUTABLE_NAME}** ({SHELL_EXECUTABLE})
— not necessarily bash despite the tool's name, configurable via config.toml's
`[bash].shell`. If it's `zsh`, a prelude already neutralizes unquoted `$var`
word-splitting and unmatched-glob hard errors, so ordinary bash syntax is safe as
written — the one real difference left is that zsh array indices are 1-based
instead of 0-based. This is purely local — it says nothing about any worker's own
shell, which may differ machine to machine and is a separate fact you'd have to ask
that worker about if it ever mattered.

Every machine has its own copy of the local capabilities. `python` here is not the
kernel a worker uses; `browser_navigate` here is not a worker's browser page;
`/memories` here is not a worker's memory. Same names, different computers, no
shared state.

Prefer the local tools only for work that genuinely belongs on this machine —
inspecting the router's own files and config, quick calculations, notes to
`/memories`, and reading anything a worker handed back. Delegate to a worker when
the task needs that machine specifically: what is installed on it, what is plugged
into it, what data lives on it, or which network it sits on. A worker exists
because it has something this machine does not, so doing its job here quietly gets
you the wrong environment. When a worker is named or implied, use it — do not
substitute a local tool because it is faster.

Workers are separate machines. They do not share a filesystem, a network view, a
clipboard, or any state — with each other or with this one. A path, a running
process, a database file, or a browser page on one worker does not exist on
another, and a path you read locally does not exist on any worker. To move data
between machines you must read it out of one and pass it into the other as part of
the task text. Never assume a worker can see something another worker produced, or
something you produced here.

Many workers are ResearchMesh instances exposing a single `delegate` tool. That is
not a command runner — it is a full agent with its own tools that runs its own
multi-step loop. Give it an outcome to achieve and the constraints that matter, not
a single command to execute. Include absolute paths, since its working directory is
its own install directory rather than anything of yours. It will use as many of its
own tools as the task needs before answering.

`delegate` takes a `session` id and keeps one conversation per id, including that
worker's Python kernel namespace and browser page. Reuse the same id when following
up on earlier work on that same worker so it still has the context; use a fresh id
to start clean. Session ids are per-worker — the same string on two workers is two
unrelated conversations. A fresh id does not clear that worker's `/memories`, which
outlives every session on it; only the kernel, browser page and conversation reset.

Running work in parallel: issuing several tool calls in one turn makes different
workers run at the same time, which is the main reason this fleet exists. Two calls
to the *same* worker do not overlap — a ResearchMesh worker has one mouse, one
browser page and one kernel, and serialises its delegations — so batch across
workers, not within one. Local tools are likewise one machine and run in order.
Split genuinely independent work; keep dependent steps in order.

Delegations are slow. A GUI or browser task can take minutes, and you get one reply
when it is finished rather than progress updates. Send a task once and wait for it.
Do not re-send it because it is taking a while, and do not poll for status.

A worker that is unreachable is reported as a failed tool result. Treat that as that
machine being down: say so, carry on with the workers that answered, and do not
silently substitute a different worker for the one that was asked for.

Report what actually happened, and attribute it. Say which worker produced which
result. If a delegation failed or returned something inconclusive, say so and include
what it returned. Never present a worker's claim as verified unless it showed you the
evidence.

`interactive_run` and a password or token prompt, on any machine: never answer it with
a plain `send` field, and never ask the user to type the real value into this
conversation, under any circumstance. This applies to every call that could touch a
secret, including ones that look trivial (`sudo whoami`) exactly the same as ones that
look consequential (`sudo apt upgrade`) — there is no size of command where typing a
real password into a `send` field or into the chat becomes acceptable. Use a step's
`send_env` (an environment variable, named only) or `send_secret` (a `pass` entry,
named only) instead — the real value is resolved locally and never has to appear in
this conversation at all.

Before asking the user to name a `send_secret` entry, check what actually exists
first: run `pass ls` yourself (via `bash` — it lists entry names only, decrypts
nothing, needs no passphrase) and show the user the real list, then ask them to pick
from it. Do not mention `pass` as a vague, hypothetical option ("if you use pass,
tell me the entry name") without having checked — that forces the user to go verify
their own setup instead of you doing the one cheap, harmless command that answers it
directly. If `pass ls` shows nothing, or `pass` is not installed at all, say that
plainly and offer `send_env` instead, or walk through the one-time `pass` setup — do
not fall back to asking for the raw value just because nothing is configured yet.
Never pick an entry yourself from that list, no matter how obvious a name looks — the
user names the exact entry for every real task, every time.
"""

# Appended to SYSTEM_PROMPT on a /dagent turn, where the local tools have been
# withheld from `tools` entirely. Without this the prompt above still describes
# local tools by name, and the model spends the turn reasoning about why `bash`
# is missing. Cheap to add: changing the tool list has already cost the cached
# prefix for this request, so the extra bytes are free.
_DELEGATE_ONLY_SUFFIX = """

THIS TURN ONLY: the local tools described above are not available to you. Every
tool in your list belongs to a worker on another machine. Do this work by
delegating it. If it genuinely cannot be done on any worker you have, say so
plainly rather than approximating it with a tool that is not the right one.
"""


def _block_field(block, name: str):
    """Read a field off a content block that may be an SDK object or a dict.

    Assistant turns hold the SDK's own block objects (straight off
    `response.content`); the tool_result turns we build ourselves are plain
    dicts. Anything walking the whole conversation has to cope with both.
    """
    if isinstance(block, dict):
        return block.get(name)
    return getattr(block, name, None)


def _orphaned_tool_uses(messages) -> list[str]:
    """tool_use ids that never got a result block — the poisoned-session check.

    The API requires every tool_use block to be answered in the *immediately
    following* message. One that isn't doesn't just break the turn it happened
    in: the block stays in the history for the life of the process, so every
    later request fails the same way, however many turns later. That failure
    reads as "it started 400ing and won't stop", which is very hard to tell
    from a context overflow without looking.

    Covers both flavors the API can leave dangling, not just the client-tool
    one:
      - a plain client `tool_use` block, answered by a `tool_result` block.
      - a `server_tool_use` (or an MCP-connector `mcp_tool_use`) block,
        answered by a tool-specific result block instead — e.g.
        `web_search_tool_result`, `web_fetch_tool_result`. This router
        exposes both as real Anthropic server tools (see
        core/claude_learned_schemas.py), so this is a real, reachable case,
        not a theoretical one. A dangling one is just as poisonous: the
        assistant turn never closed, so the next request 400s the same way
        a missing client tool_result does. Matched generically by suffix
        (`_tool_use` / `_tool_result`) rather than a hardcoded list of
        current tool names, so a future server tool is covered without
        editing this function again.

    Both flavors pair up by the same id field regardless of which specific
    block type is involved — confirmed against Anthropic's own docs: "A
    server_tool_use block and its result block pair up by tool_use_id, not
    by position."

    `Chat._finalize_turn` exists to make this list empty by the time any
    turn ends; this is how you find out it didn't. Ported from ResearchMesh
    core/chat.py (commit 672aae1) — see that repo's dev log for the full
    production-error history this closes out.
    """
    answered: set[str] = set()
    issued: list[str] = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            kind = _block_field(block, "type")
            if not kind:
                continue
            if kind == "tool_use" or kind.endswith("_tool_use"):
                block_id = _block_field(block, "id")
                if block_id:
                    issued.append(block_id)
            elif kind == "tool_result" or kind.endswith("_tool_result"):
                used = _block_field(block, "tool_use_id")
                if used:
                    answered.add(used)
    return [i for i in issued if i not in answered]


def _classify_orphans(messages) -> tuple[list[str], list[str]]:
    """Split `_orphaned_tool_uses`'s output by whether each id is mechanically
    fixable or not.

    A plain client `tool_use` block can always be closed out with a synthetic
    error `tool_result` — that's what makes it "client-flavored" here. A
    `server_tool_use`/`mcp_tool_use` block (web_search, web_fetch, an MCP
    connector tool) cannot: the API expects a type-specific result block
    (`web_search_tool_result`, etc.) that this app never had the real data
    for, since the server ran it, not us. Returns (client_ids, server_ids).
    """
    ids = _orphaned_tool_uses(messages)
    if not ids:
        return [], []
    orphan_set = set(ids)
    client_ids: list[str] = []
    server_ids: list[str] = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            block_id = _block_field(block, "id")
            if block_id not in orphan_set:
                continue
            kind = _block_field(block, "type")
            if kind == "tool_use":
                client_ids.append(block_id)
            elif kind and kind.endswith("_tool_use"):
                server_ids.append(block_id)
    return client_ids, server_ids


def _duplicate_tool_result_ids(messages) -> dict[str, int]:
    """tool_use_ids answered by MORE than one tool_result-family block.

    The literal API error is `invalid_request_error: ... each tool_use must
    have a single result. Found multiple tool_result blocks with id: <id>`
    — confirmed hit for real in production, on ResearchMesh (this router's
    sibling client, same core/chat.py lineage before this port). Returns
    {id: count}.
    """
    counts: dict[str, int] = {}
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            kind = _block_field(block, "type")
            if not kind:
                continue
            if kind == "tool_result" or kind.endswith("_tool_result"):
                used = _block_field(block, "tool_use_id")
                if used:
                    counts[used] = counts.get(used, 0) + 1
    return {k: v for k, v in counts.items() if v > 1}


def _dedupe_duplicate_tool_results(messages) -> int:
    """Mutates `messages` in place: for any tool_use_id with more than one
    tool_result-family block answering it, keep only the FIRST one seen (in
    message order — the real one from genuine tool execution) and drop the
    rest (synthetic duplicates from the now-fixed iteration-cutoff bug, or
    any other stray duplicate).

    Removes just the offending blocks from whichever message's content list
    holds them, not whole messages — never a bulk deletion. Returns how many
    blocks were removed.
    """
    dup_counts = _duplicate_tool_result_ids(messages)
    if not dup_counts:
        return 0
    seen: set[str] = set()
    removed = 0
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        kept = []
        for block in content:
            kind = _block_field(block, "type")
            is_result = bool(kind) and (kind == "tool_result" or kind.endswith("_tool_result"))
            used = _block_field(block, "tool_use_id") if is_result else None
            if is_result and used in dup_counts:
                if used in seen:
                    removed += 1
                    continue
                seen.add(used)
            kept.append(block)
        message["content"] = kept
    return removed


def _excise_dangling_blocks(messages, ids: set[str]) -> int:
    """Mutates `messages` in place: removes any block whose `id` is in `ids`
    — used only for a `server_tool_use`/`mcp_tool_use` orphan, where no
    synthetic result block satisfies the API's schema for that tool type.

    Removes only the specific dangling block(s), never the message that
    holds them (any other content in that message — text, other blocks — is
    kept) and never any other message. This is the minimal possible repair:
    contrast with wiping a turn or a conversation, neither of which this file
    does anywhere. Returns how many blocks were removed.
    """
    if not ids:
        return 0
    removed = 0
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        kept = []
        for block in content:
            block_id = _block_field(block, "id")
            if block_id in ids:
                removed += 1
                continue
            kept.append(block)
        message["content"] = kept
    return removed


def _answer_orphaned_client_tool_uses(
    messages, client_ids: list[str], content: str
) -> None:
    """Mutates `messages` in place: answers each id in `client_ids` with a
    synthetic error `tool_result`, placed so it is genuinely part of the
    message immediately following the specific message that holds that
    tool_use — never simply appended to the tail of `messages`.

    Ported from ResearchMesh core/chat.py — same underlying bug found and
    fixed there: appending to the tail (the previous behavior here too, via
    `claude_service.add_user_message`) is only correct if the orphan happens
    to already be the very last thing in the conversation. It silently stops
    being correct the moment anything else has already been appended after
    the orphaning message — most commonly a plain new user query, added by
    `run()`'s own next call before this repair ever runs. Confirmed live in
    production on ResearchMesh (this router's sibling client, same
    core/chat.py lineage): the repair ran, reported success ("answered 1
    orphaned tool_use block"), and the retried request 400'd on the exact
    same id it had supposedly just answered — because the synthetic result
    landed one message too late, still leaving the tool_use followed by a
    plain user-text message instead of its own answer. Reproduced exactly
    outside production too (`_orphaned_tool_uses` came back empty after
    that "successful" repair — it only checks "answered somewhere later,"
    not the API's actual stricter "immediately after" rule, which is why
    the old code believed it had fixed something it hadn't).

    If the message right after the orphaning one is already a `user`
    message, the synthetic result(s) are merged into the FRONT of its
    existing content (mixing tool_result blocks with other content in one
    user turn is a normal, documented shape) — this also avoids ever
    creating two consecutive `user`-role messages. Only if there is no
    following message at all is a new one inserted, matching the original
    behavior for the case it was actually correct for.

    Groups ids by which message actually holds them (usually one, but not
    guaranteed) and processes messages back-to-front so an earlier
    insertion never shifts the index of a later one out from under it.
    """
    if not client_ids:
        return
    orphan_set = set(client_ids)
    by_index: dict[int, list[str]] = {}
    for idx, message in enumerate(messages):
        content_list = message.get("content")
        if not isinstance(content_list, list):
            continue
        for block in content_list:
            if _block_field(block, "type") != "tool_use":
                continue
            block_id = _block_field(block, "id")
            if block_id in orphan_set:
                by_index.setdefault(idx, []).append(block_id)

    for idx in sorted(by_index, reverse=True):
        results = [
            {
                "type": "tool_result",
                "tool_use_id": i,
                "content": content,
                "is_error": True,
            }
            for i in by_index[idx]
        ]
        next_idx = idx + 1
        if next_idx < len(messages) and messages[next_idx].get("role") == "user":
            existing = messages[next_idx].get("content")
            if isinstance(existing, str):
                existing = [{"type": "text", "text": existing}]
            elif not isinstance(existing, list):
                existing = []
            messages[next_idx]["content"] = results + existing
        else:
            messages.insert(next_idx, {"role": "user", "content": results})


def _approx_size(messages) -> tuple[int, int]:
    """(message count, character count) for the conversation.

    Deliberately a character count rather than a real token count:
    `count_tokens` cannot measure this conversation at all, because
    `web_search`/`web_fetch` are server tools and that endpoint rejects them
    outright. Roughly 3-4 characters per token is close enough to tell "nowhere
    near the window" from "at it", which is the only question being asked here.
    """
    chars = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            chars += len(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    chars += len(str(block.get("content") or block.get("text") or ""))
                else:
                    chars += len(str(getattr(block, "text", "") or ""))
    return len(messages), chars


def _report_usage(response) -> None:
    """One line of token accounting. From the second request onward, cache read
    should be large and cache write near zero — that means the prefix is being
    reused. Cache read staying at 0 means the breakpoint isn't landing."""
    usage = response.usage
    print(
        "[usage: input {} | cache write {} | cache read {} | output {}]".format(
            usage.input_tokens,
            getattr(usage, "cache_creation_input_tokens", 0) or 0,
            getattr(usage, "cache_read_input_tokens", 0) or 0,
            usage.output_tokens,
        )
    )


def _local_result_to_content(local):
    """Local tool executors normally return a plain string. They can also return
    the image marker built by core.output.image_result ({"__kind__": "image",
    ...}) — the file editor's and memory's `view` on an image file, and every
    computer-use screenshot — which we translate into a real tool_result content
    list carrying an `image` block, so the model actually receives pixels
    instead of a UTF-8 decode error.

    The worker side of this lives in core/tools.py `_call_one`, which builds the
    same shape out of MCP `ImageContent`."""
    if isinstance(local, dict) and local.get("__kind__") == "image":
        return [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": local["media_type"],
                    "data": local["data"],
                },
            },
            {"type": "text", "text": local["text"]},
        ]
    return local


class Chat:
    def __init__(
        self,
        claude_service: Claude,
        clients: Mapping[str, Worker],
        descriptions: dict[str, str] | None = None,
        max_parallel: int = 8,
    ):
        self.claude_service: Claude = claude_service
        # `Mapping[str, Worker]`, not `dict[str, MCPClient]`, for the reason
        # core/tools.py spells out: the bridge only needs list_tools/call_tool,
        # and dict is invariant in its value type, so `dict[str, MCPClient]`
        # would not satisfy `dict[str, Worker]` on the way through to
        # ToolManager.build.
        self.clients: Mapping[str, Worker] = clients
        # worker id -> the routing blurb from its config.toml entry
        self.descriptions: dict[str, str] = descriptions or {}
        self.max_parallel: int = max_parallel
        self.messages: list[MessageParam] = []
        self._announced = False

    async def _run_tool_uses(self, message, index: ToolIndex) -> list:
        """Route each tool_use block: local executor, or the MCP ToolManager.

        Every tool_use block owes the API a matching tool_result in the very
        next message, no exceptions. `execute_blocks` guarantees that for the
        worker side; the local branch below has to guarantee it for itself,
        which is why the `except` is blanket and per-block rather than around
        the loop. A local executor that raised and aborted the batch would
        orphan its own block *and* every block after it.

        Results are reassembled in the original block order. The API does not
        require it, but `execute_blocks` promises it for worker blocks and a
        transcript where the results track the calls is worth keeping — routing
        locals out of the list and appending them back would otherwise reorder
        every mixed turn.
        """
        blocks = [b for b in message.content if b.type == "tool_use"]
        by_id: dict[str, ToolResultBlockParam] = {}
        worker_blocks: list = []

        for block in blocks:
            try:
                local = await local_tools.execute(block.name, block.input)
            except Exception as e:
                print(f"[local tool '{block.name}' raised: {e}]")
                by_id[block.id] = {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": f"Error executing tool '{block.name}': {e}",
                    "is_error": True,
                }
                continue

            # `None` means no local module owns that name — it belongs to a
            # worker (or to nothing at all, which execute_blocks answers with
            # "Could not find that tool").
            if local is None:
                worker_blocks.append(block)
                continue

            by_id[block.id] = {
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": _local_result_to_content(local),
            }

        if worker_blocks:
            for block, result in zip(
                worker_blocks,
                await ToolManager.execute_blocks(
                    index, worker_blocks, max_parallel=self.max_parallel
                ),
            ):
                by_id[block.id] = result

        return [by_id[block.id] for block in blocks]

    def _finalize_turn(self, reason: str) -> str:
        """Close out a turn that's ending abnormally. Always re-scans the
        live `self.messages` for what's actually still dangling — never
        trusts a cached `response` object (that was the source of a real
        duplicate-tool_result bug on ResearchMesh, this router's sibling
        client — same core/chat.py lineage before this port; see that
        repo's dev log for the full production-error history).

        Never deletes a turn, a message, or the conversation — only the
        specific dangling block(s), each in the minimal way its flavor
        allows: client `tool_use` gets a synthetic error `tool_result`;
        server-flavored (`server_tool_use`/`mcp_tool_use`) has no valid
        synthetic result, so the block itself is excised in place.

        Replaces the old `_resolve_pending_tool_uses` (only ever handled
        the client-tool_use case, and only when `stop_reason == "tool_use"`
        — a dangling server_tool_use at the iteration cutoff during an
        open pause_turn was never resolved at all).

        Returns the text to show the user.
        """
        client_ids, server_ids = _classify_orphans(self.messages)
        if client_ids:
            _answer_orphaned_client_tool_uses(
                self.messages, client_ids, f"[{reason}]"
            )
        base = f"[{reason}]"
        if server_ids:
            _excise_dangling_blocks(self.messages, set(server_ids))
            base += (
                f" {len(server_ids)} background tool call"
                f"{'s' if len(server_ids) != 1 else ''} that never finished "
                f"{'were' if len(server_ids) != 1 else 'was'} removed so the "
                "conversation can continue — nothing else was touched."
            )
        return base

    def clear(self) -> str:
        """`/clear` — drop the conversation, keep the process and the fleet.

        The only recovery path from a poisoned history. `self.messages` lives
        for the life of the process, so both of the failures that persist —
        an unanswered tool_use block, and a conversation that has outgrown the
        context window — leave every subsequent turn failing identically. Before
        this existed the only way out was killing the router, which also drops
        every worker connection and every worker's session id with it.

        Deliberately does not touch `self.clients`: the fleet is unrelated to
        the conversation, and reconnecting three machines to recover from a bad
        turn would be the expensive half of a restart for none of the benefit.
        """
        count, chars = _approx_size(self.messages)
        orphans = _orphaned_tool_uses(self.messages)
        self.messages = []
        detail = f"cleared {count} messages (~{chars:,} chars)"
        if orphans:
            detail += (
                f" — including {len(orphans)} unanswered tool_use block"
                f"{'s' if len(orphans) != 1 else ''}, which is what was "
                f"breaking every turn"
            )
        return f"[{detail}]"

    def _report_api_failure(
        self, error: Exception, repair_attempted: str | None = None
    ) -> None:
        """Say which failure this is, rather than leaving it to guesswork.

        By the time this runs, `_call_chat_with_auto_repair` has already
        tried the one fully-mechanical repair this app knows how to do
        (dedupe/answer/excise dangling tool blocks — see
        `_auto_repair_poisoned_history`) and retried once.
        `repair_attempted` carries what it found and fixed, if anything.

        This never recommends `/clear`, or any other action that discards
        conversation content, anywhere — deliberately (ported from
        ResearchMesh core/chat.py commit 672aae1, whose whole point was
        removing exactly that from every automated path — `/clear` still
        exists as the manual command above, untouched). If nothing here
        could be mechanically repaired, the honest thing to do is report the
        facts (the error, the size, any orphans still present after the
        repair attempt) and leave the decision to the user, not prescribe a
        destructive default.
        """
        text = str(error)
        count, chars = _approx_size(self.messages)
        orphans = _orphaned_tool_uses(self.messages)

        print(f"[api error] {text}")
        print(f"[api error] conversation: {count} messages, ~{chars:,} chars")

        if repair_attempted:
            print(
                f"[api error] an automatic repair ran first ({repair_attempted}), "
                "but the retried request still failed — this is a different, "
                "unrelated problem."
            )

        if orphans:
            print(
                f"[api error] {len(orphans)} unanswered tool_use block(s) "
                f"still present after the repair attempt: "
                f"{', '.join(orphans[:3])}"
                f"{' …' if len(orphans) > 3 else ''}"
            )
        elif "too long" in text.lower() or "context" in text.lower():
            print(
                "[api error] the conversation has outgrown the context window."
            )

    def _auto_repair_poisoned_history(self) -> str | None:
        """Mechanical, unconditionally-safe repair of `self.messages`,
        tried whenever a real `chat()` call raises: dedupe any duplicate
        tool_result, answer any orphaned client tool_use, excise any
        orphaned server-flavored block. Touches only the offending blocks,
        never a whole message or the conversation.

        Ported from ResearchMesh core/chat.py (commit 672aae1).

        Returns a short description of what was repaired, or None if there
        was nothing here to fix (a real network/auth error, a genuine
        context-window overflow, or some other cause).
        """
        repairs: list[str] = []

        removed_dupes = _dedupe_duplicate_tool_results(self.messages)
        if removed_dupes:
            repairs.append(
                f"removed {removed_dupes} duplicate tool_result block"
                f"{'s' if removed_dupes != 1 else ''}"
            )

        client_ids, server_ids = _classify_orphans(self.messages)
        if client_ids:
            _answer_orphaned_client_tool_uses(
                self.messages,
                client_ids,
                "[repaired: this tool_use was never answered]",
            )
            repairs.append(
                f"answered {len(client_ids)} orphaned tool_use block"
                f"{'s' if len(client_ids) != 1 else ''}"
            )
        if server_ids:
            _excise_dangling_blocks(self.messages, set(server_ids))
            repairs.append(
                f"removed {len(server_ids)} dangling background tool block"
                f"{'s' if len(server_ids) != 1 else ''} (no synthetic fix "
                f"exists for these)"
            )

        return "; ".join(repairs) if repairs else None

    def _call_chat_with_auto_repair(self, system, tool_defs, thinking):
        """The one real `chat()` call site: on failure, try
        `_auto_repair_poisoned_history` and retry once. Returns the
        response on success (either attempt), or None if both raised
        (`_report_api_failure` already called in that case).

        Ported from ResearchMesh core/chat.py (commit 672aae1). `system` is
        an explicit parameter here (unlike ResearchMesh's hardcoded
        SYSTEM_PROMPT) because this router's system prompt varies per turn
        (the `/dagent` remote_only branch appends _DELEGATE_ONLY_SUFFIX).
        """
        try:
            return self.claude_service.chat(
                messages=self.messages,
                system=system,
                tools=tool_defs,
                thinking=thinking,
            )
        except Exception as e:
            repair = self._auto_repair_poisoned_history()
            if repair is None:
                self._report_api_failure(e)
                return None
            print(f"[api error] {e}")
            print(f"[api error] auto-repaired the conversation history: {repair}")
            print("[api error] retrying this request once...")
            try:
                return self.claude_service.chat(
                    messages=self.messages,
                    system=system,
                    tools=tool_defs,
                    thinking=thinking,
                )
            except Exception as e2:
                self._report_api_failure(e2, repair_attempted=repair)
                return None

    async def workers_listing(self) -> str:
        """`/workers` — the fleet that is up, and the names `/dagent` takes.

        Deliberately only what is reachable. A worker that failed to connect or
        has died since is simply absent, which is the same answer the model gets
        when it tries to call one: that machine is not available this turn.

        Costs one `list_tools` round trip per worker, which is the point — a
        cached listing could report a machine that went down ten minutes ago.
        """
        index = await ToolManager.build(
            self.clients,
            self.descriptions,
            reserved={t["name"] for t in local_tools.TOOLS},
        )
        worker_ids = index.worker_ids()
        if not worker_ids:
            return "[no workers up]"

        width = max(len(w) for w in worker_ids)
        lines = []
        for worker_id in worker_ids:
            count = len(index.defs_for(worker_id))
            blurb = self.descriptions.get(worker_id, "").strip() or "—"
            lines.append(
                f"  {worker_id:<{width}}  "
                f"{count} tool{'s' if count != 1 else ''}  {blurb}"
            )
        return f"{index.summary()} up\n" + "\n".join(lines)

    def split_worker(self, query: str) -> tuple[str | None, str]:
        """Peel a leading worker name off a `/dagent` request.

        `gpu-box render the scene` -> ("gpu-box", "render the scene"), but only
        when that first word is actually a configured worker. Otherwise the
        whole string is the request — a task legitimately starting with a word
        that happens to look like a name must not be silently truncated.
        """
        head, _, rest = query.strip().partition(" ")
        if head in self.clients and rest.strip():
            return head, rest.strip()
        return None, query.strip()

    def resolve_worker_model_request(
        self, sub: str, arg: str
    ) -> tuple[str, dict[str, str] | None, str | None] | None:
        """Parse `/model <sub> <arg>` to decide whether `sub` names a
        CONNECTED worker and, if so, what request that implies for its
        `model` MCP tool.

        This is the precedence-critical half of `/model`'s worker-reach-in
        branch: `sub in self.clients` is what makes worker-name-wins the rule
        over subcommand-name-wins — e.g. a worker literally named "swap" is
        still treated as a worker target, never confused with the router's
        own `/model swap <name/index>` subcommand. Extracted as its own
        method (mirroring `split_worker()` above) specifically so this
        precedence decision is unit-testable without a live REPL or a real
        MCP round trip — see smoke_test.py's
        check_model_worker_dispatch().

        Returns:
          None
            `sub` is not a connected worker id. The caller should ignore
            `arg` entirely and fall through to the router's OWN bare
            `/model` handling instead.
          (worker_id, None, error_text)
            `sub` IS a worker, but `arg` didn't parse into a valid request
            (a missing swap target, or an unrecognized subcommand).
            `error_text` is the exact, ready-to-print rejection message.
            The caller should print it and make NO MCP call.
          (worker_id, arguments, None)
            `sub` IS a worker with a fully valid request. `arguments` is
            exactly the dict to pass to `client.call_tool("model", ...)` —
            the caller performs that (fallible) call next.
        """
        if sub not in self.clients:
            return None

        worker_id = sub
        wsub_parts = arg.split(None, 1) if arg else []
        wsub = wsub_parts[0] if wsub_parts else ""
        warg = wsub_parts[1].strip() if len(wsub_parts) > 1 else ""

        if not wsub:
            return worker_id, {"action": "list"}, None
        if wsub == "swap":
            if not warg:
                return (
                    worker_id,
                    None,
                    f"[usage: /model {worker_id} swap <name or index>]",
                )
            return worker_id, {"action": "swap", "arg": warg}, None
        return (
            worker_id,
            None,
            (
                f"[worker: {worker_id}] unrecognized subcommand {wsub!r} — use "
                f"/model {worker_id} or /model {worker_id} swap <name/index>"
            ),
        )

    async def call_worker_model(
        self, worker_id: str, arguments: dict[str, str]
    ) -> str:
        """Actually invoke a connected worker's `model` MCP tool and format
        the result — the other half of `/model`'s worker-reach-in branch,
        paired with `resolve_worker_model_request()` above.

        Any transport/protocol error from the call is caught and reported,
        never raised — same reject-don't-crash posture as every other
        worker-facing path in this file. Extracted as its own method for the
        same testability reason as `resolve_worker_model_request()`: a
        FakeWorker can script a response OR an exception here without any
        real MCP process or live REPL involved.
        """
        client = self.clients[worker_id]
        try:
            result = await client.call_tool("model", arguments)
        except Exception as e:
            return f"[worker: {worker_id}] model tool call failed: {e}"

        texts = (
            [b.text for b in result.content if isinstance(b, TextContent)]
            if result and result.content
            else []
        )
        body = "\n".join(texts) or (
            "(no output)"
            if not (result and result.is_error)
            else "(error, no message text)"
        )
        return f"[worker: {worker_id}] {body}"

    async def run(
        self,
        query: str,
        thinking: bool = False,
        remote_only: bool = False,
        worker: str | None = None,
    ) -> str:
        """One user turn.

        `remote_only` drops the local tools from the request entirely, and
        `worker` narrows it further to one machine's tools. Both are enforced by
        *withholding the schemas*, not by instructing the model — the whole
        difficulty this addresses is that a local `bash` is faster and more
        directly matched to any concrete command than a `delegate` that takes
        minutes, so an instruction is a preference and an absent tool is a fact.

        The cost is a cache miss in each direction: tools render ahead of
        `system` in the cached prefix, so changing the list invalidates
        everything after it on the `/dagent` turn and again on the next ordinary
        one. That is worth it against a delegation silently executed on the
        wrong machine.
        """
        final_text_response = ""
        self.claude_service.add_user_message(self.messages, query)

        # Built once per user turn, not once per tool-use iteration. The fleet
        # can't change mid-turn, and re-deriving it was a `list_tools` round
        # trip per worker per loop pass — over the network, up to
        # MAX_TOOL_ITERATIONS times for a single question.
        index = await ToolManager.build(
            self.clients,
            self.descriptions,
            reserved={t["name"] for t in local_tools.TOOLS},
        )

        if remote_only:
            # The local half is withheld, not discouraged. `worker` narrows it
            # to one machine.
            tool_defs = (
                index.defs_for(worker) if worker else list(index.tool_defs)
            )
            system = SYSTEM_PROMPT + _DELEGATE_ONLY_SUFFIX

            # Nothing to delegate to. Withholding the local tools *and* having
            # no worker tools would send a turn with no tools at all, which the
            # model answers from thin air — the one outcome /dagent exists to
            # rule out. Bail before spending the request, and unwind the user
            # message so the aborted turn leaves no trace in self.messages.
            if not tool_defs:
                self.messages.pop()
                target = f"worker '{worker}'" if worker else "no worker"
                return (
                    f"[{target} is up but has no tools — nothing to delegate "
                    f"to. /workers lists the fleet]"
                    if worker
                    else "[no workers up — /workers lists the fleet]"
                )
        else:
            # Local tools first, deliberately. Tools render ahead of `system` in
            # the cached prefix, and this half is static while `index.tool_defs`
            # is rebuilt from whichever workers answered — so putting the fixed
            # list first keeps the front of the prefix identical when a worker
            # drops out mid-session, instead of shifting everything after it.
            tool_defs = local_tools.TOOLS + index.tool_defs
            system = SYSTEM_PROMPT

        if not self._announced:
            print(
                f"[router] {len(local_tools.TOOLS)} local tools, "
                f"{index.summary()}"
            )
            self._announced = True
        if remote_only:
            target = worker or "the fleet"
            print(f"[router] delegate-only turn — {target}, no local tools")
        if not index.tool_defs:
            print(
                "[router] no worker tools available — local tools only, "
                "no fleet"
            )

        # Index of this turn's own assistant message while a pause_turn
        # continuation is open. Anthropic's own reference implementation
        # REPLACES this slot on each continuation rather than appending a
        # sibling message — an unconditional append here (the pre-port
        # behavior) stacks multiple consecutive assistant-role messages
        # with zero user messages between them across a multi-continuation
        # pause_turn sequence, a real contract violation independent of
        # the duplicate-tool_result bug below. Ported from ResearchMesh
        # core/chat.py (commit 672aae1) — see that repo's dev log for the
        # full production-error history this closes out.
        pending_pause_turn_idx: int | None = None

        iterations = 0
        extra_continuations = 0
        # Set only for the two cases where the next chat() call is
        # unconditionally required by the API: an open pause_turn, or a
        # server_tool_use left dangling by a mixed tool_use response.
        # Reset every pass so the grace budget below is never spent on an
        # ordinary continuation once the main budget runs out.
        mandatory_continuation = False
        while True:
            if iterations >= MAX_TOOL_ITERATIONS:
                if not mandatory_continuation:
                    final_text_response = "[stopped: exceeded tool-iteration limit]"
                    break
                if extra_continuations >= EXTRA_CONTINUATION_LIMIT:
                    final_text_response = self._finalize_turn(
                        "stopped: exceeded tool-iteration limit"
                    )
                    break
                extra_continuations += 1
            else:
                iterations += 1
            mandatory_continuation = False

            response = self._call_chat_with_auto_repair(system, tool_defs, thinking)
            if response is None:
                return "[api error: chat request failed]"
            if SHOW_USAGE:
                _report_usage(response)
            if thinking:
                thought = [b for b in response.content if b.type == "thinking"]
                print(f"[thinking blocks: {len(thought)}]")

            if pending_pause_turn_idx is not None:
                self.messages[pending_pause_turn_idx]["content"] = response.content
            else:
                self.claude_service.add_assistant_message(self.messages, response)

            if response.stop_reason == "pause_turn":
                # A worker-side pause; resend the conversation to resume it.
                if pending_pause_turn_idx is None:
                    pending_pause_turn_idx = len(self.messages) - 1
                mandatory_continuation = True
                continue

            pending_pause_turn_idx = None

            if response.stop_reason == "tool_use":
                print(self.claude_service.text_from_message(response))
                try:
                    tool_result_parts = await self._run_tool_uses(
                        response, index
                    )
                except Exception as e:
                    # Genuinely unanswered — first time seeing these blocks,
                    # not a re-resolution.
                    print(f"[tool routing error: {e}]")
                    final_text_response = self._finalize_turn(
                        f"tool execution failed: {e}"
                    )
                    break
                self.claude_service.add_user_message(
                    self.messages, tool_result_parts
                )

                # A dangling server_tool_use here means the API owes us one
                # more mandatory round trip to resolve it (Server tools doc).
                _, server_ids = _classify_orphans(self.messages)
                if server_ids:
                    mandatory_continuation = True
                    continue

                if iterations >= MAX_TOOL_ITERATIONS:
                    final_text_response = "[stopped: exceeded tool-iteration limit]"
                    break
                continue

            # Anything else (end_turn, stop_sequence, and critically
            # max_tokens) falls through here. A max_tokens cutoff that hit
            # mid-tool_use — e.g. a single large `create` call whose file
            # content ran past the token budget — still gets its content
            # appended above like any other assistant turn, tool_use block
            # included, but stop_reason is "max_tokens", not "tool_use", so
            # nothing above ever routed/answered it. Left alone, that
            # tool_use sits unresolved past the end of this run() call and
            # poisons every later turn (ported from ResearchMesh core/chat.py
            # — see that repo's dev log for the full incident). Re-check the
            # live message list rather than trusting stop_reason alone, and
            # finalize instead of returning as if this were an ordinary
            # finished turn.
            if _orphaned_tool_uses(self.messages):
                final_text_response = self._finalize_turn(
                    f"stopped: response ended early (stop_reason="
                    f"{response.stop_reason!r}) with an unresolved tool_use"
                )
                break

            final_text_response = self.claude_service.text_from_message(
                response
            )
            break

        return final_text_response
