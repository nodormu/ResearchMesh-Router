import os
from collections.abc import Mapping
from pathlib import Path

from anthropic.types import MessageParam, ToolResultBlockParam
from mcp.types import TextContent

from core import local_tools, processes

# Names taken by this router's local tools, for the collision guard in
# ToolManager.build. A toolset entry has no "name", so entries without one are
# skipped.
_LOCAL_TOOL_NAMES = {t["name"] for t in local_tools.TOOLS if "name" in t}
from core.claude import Claude
from core.claude_learned_schemas import SHELL_EXECUTABLE
from core.tools import ToolIndex, ToolManager, Worker

# Name of the interpreter the local `bash` tool runs commands through (e.g.
# "bash", "zsh"); interpolated into SYSTEM_PROMPT.
_SHELL_EXECUTABLE_NAME = Path(SHELL_EXECUTABLE).name

# Cap on chat-loop rounds per user message. A round can batch several tool_use
# calls. Resets every user message.
MAX_TOOL_ITERATIONS = 200

# Extra rounds for continuations the API requires: an open pause_turn, or a
# server_tool_use left dangling by a mixed tool_use response. See
# Chat._finalize_turn for what happens when this runs out.
EXTRA_CONTINUATION_LIMIT = 5

# Set CLAUDE_SHOW_USAGE=1 to print token and cache counters per request. A
# cache miss raises no error, so this is how to check the cache_control
# breakpoint in core/claude.py.
SHOW_USAGE = os.getenv("CLAUDE_SHOW_USAGE") == "1"

# Sent as the `system` parameter on every request. States what the schemas
# cannot: which tools run here and which on workers, and that workers are
# separate computers.
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

Browsing. `web_fetch` reads one known document. `browser_navigate` is for anything
that needs rendering, links, forms or a login. It starts headless, and when a
Cloudflare human check stops a fresh visit it reopens itself in `virtual` mode (a
hidden display). If the report still says `Human check: pending`, navigate again
with `mode: real` (the user's installed Chrome in a visible window they can click
in) or ask the user to click it. A `profile` name keeps logins between sessions.
`computer` drives the desktop itself: use it for native apps and for a browser
window the user already has open, with their own profile and logins. Put the
right window in front with `desktop_window` before typing, and get a button's
click position from `screen_find` (OCR, accurate) instead of estimating it from
a screenshot. `computer` has no vault option, so for a password in a window of
that kind ask the user to type it.

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

The same rule applies to `browser_fill` on a password or long-lived token field: never put the
real value in `value`. Use `value_secret` (a `pass` entry name), with the same entry
check and prompt shape below. Delegated task text is sent like any other message, so
never put a password in it; a login that needs a vault secret runs in this machine's
own browser.

A one-time code (from an authenticator app, SMS or email) is not a vault secret: it
expires in seconds and is useless afterwards. The user may paste it into the chat, and
when they do, type it at once with `browser_fill` `value` and `submit: true`. Never ask
the user to type it into the browser themselves; they may be unable to use a keyboard
or mouse, which is what you are here to cover. If the site says the code did not
verify, ask for a fresh one and fill it at once, with nothing else in between. The
vault rules above are for passwords and other long-lived secrets.

Before asking the user to name a `send_secret` entry, check what actually exists
first: run `pass ls` yourself (via `bash` — it lists entry names only, decrypts
nothing, needs no passphrase). Then say exactly this shape, nothing more elaborate:

please select the cred name I need to use:
<one name per line, exactly what `pass ls` printed>

Do not wrap this in a longer explanation, do not mention `pass` as a vague,
hypothetical option ("if you use pass, tell me the entry name") without having
checked, and do not add reasoning about why you're asking — the short prompt above,
with the real names from `pass ls`, is the complete response. If `pass ls` shows
nothing, or `pass` is not installed at all, say that plainly and offer `send_env`
instead, or walk through the one-time `pass` setup — do not fall back to asking for
the raw value just because nothing is configured yet. Never pick an entry yourself
from that list, no matter how obvious a name looks — the user names the exact entry
for every real task, every time.
"""

# Appended to SYSTEM_PROMPT on a /dagent turn, where local tools are withheld
# from `tools`; without it the prompt still names tools the model cannot call.
_DELEGATE_ONLY_SUFFIX = """

THIS TURN ONLY: the local tools described above are not available to you. Every
tool in your list belongs to a worker on another machine. Do this work by
delegating it. If it genuinely cannot be done on any worker you have, say so
plainly rather than approximating it with a tool that is not the right one.
"""


def _block_field(block, name: str):
    """Read a field from a content block that may be an SDK object or a dict.

    Assistant turns hold SDK objects; tool_result turns built here are plain
    dicts.
    """
    if isinstance(block, dict):
        return block.get(name)
    return getattr(block, name, None)


def _orphaned_tool_uses(messages) -> list[str]:
    """tool_use ids that never got a result block.

    The API requires each tool_use to be answered in the next message; an
    unanswered one fails every later request. Covers client `tool_use`
    (answered by `tool_result`) and server-flavored
    `server_tool_use`/`mcp_tool_use` (answered by a type-specific result
    block). Pairs by id and matches by the `_tool_use`/`_tool_result` suffix,
    so new server tools need no edit here.
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
    """Split `_orphaned_tool_uses` into (client_ids, server_ids).

    A client `tool_use` can be closed with a synthetic error `tool_result`. A
    server-flavored block cannot: its result block was never ours to build.
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
    """tool_use ids answered by more than one tool_result-family block, as {id:
    count}.

    The API rejects these: `each tool_use must have a single result`.
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
    """Remove duplicate tool_result blocks in place, keeping the first per id.
    Returns the count removed.

    Drops blocks, never messages.
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
    """Remove blocks whose `id` is in `ids`, in place. Returns the count
    removed.

    Used for server-flavored orphans, which no synthetic result can satisfy.
    The message holding a block is kept.
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
    """Answer each id in `client_ids` with a synthetic error `tool_result`, in
    place.

    The result goes in the message immediately after the one holding the
    tool_use, which is what the API checks; appending to the end is wrong once
    anything follows. A following `user` message gets the results merged at the
    front of its content; otherwise a new message is inserted. Messages are
    processed back to front so insertions do not shift later indexes.
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

    Characters, not tokens: `count_tokens` rejects the server tools
    (`web_search`, `web_fetch`) this conversation contains.
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
    """Local tool executors return a string, or the image marker from
    core.output.image_result (file `view` on an image, every computer
    screenshot), which becomes a tool_result content list with an `image`
    block.

    Worker results are built the same way in core/tools.py `_call_one`.
    """
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
        """Route each tool_use block to a local executor or the MCP
        ToolManager.

        Every block must get a tool_result in the next message, so the local
        branch catches exceptions per block. Results keep the original block
        order.
        """
        blocks = [b for b in message.content if b.type == "tool_use"]
        by_id: dict[str, ToolResultBlockParam] = {}
        worker_blocks: list = []

        for block in blocks:
            # For a computer-toolset member, `block.toolset_name` is "computer"
            # (None otherwise); the tool_result must echo it or the API rejects
            # the batch.
            toolset_name = getattr(block, "toolset_name", None)

            try:
                local = await local_tools.execute(block.name, block.input)
            except Exception as e:
                print(f"[local tool '{block.name}' raised: {e}]")
                result: ToolResultBlockParam = {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": f"Error executing tool '{block.name}': {e}",
                    "is_error": True,
                }
                if toolset_name is not None:
                    result["toolset_name"] = toolset_name
                by_id[block.id] = result
                continue

            # `None` means no local module owns that name — it belongs to a
            # worker (or to nothing at all, which execute_blocks answers with
            # "Could not find that tool").
            if local is None:
                worker_blocks.append(block)
                continue

            result = {
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": _local_result_to_content(local),
            }
            if toolset_name is not None:
                result["toolset_name"] = toolset_name
            by_id[block.id] = result

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
        """Close out a turn that ends abnormally.

        Re-scans `self.messages` for what is still dangling. Never deletes a
        turn or message: a client `tool_use` gets a synthetic error
        `tool_result`, a server-flavored block is removed. Returns the text to
        show the user.
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
        """`/clear`: drop the conversation, keep the process and the fleet.

        `self.messages` lives for the whole process, so a poisoned or oversized
        history fails every later turn; this is the way out. `self.clients` is
        untouched.
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
        """Say which failure this is.

        `_call_chat_with_auto_repair` has already tried the mechanical repair
        and retried once; `repair_attempted` is what it found. This reports
        facts (error, size, remaining orphans) and never recommends `/clear` or
        any step that discards conversation.
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
        """Repair `self.messages` after a failed `chat()`: dedupe tool_results,
        answer orphaned client tool_uses, remove orphaned server-flavored
        blocks.

        Touches only the offending blocks. Returns a short description, or None
        if there was nothing to fix.
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
        """The one `chat()` call site: on failure, try
        `_auto_repair_poisoned_history` and retry once.

        Returns the response, or None if both attempts raised
        (`_report_api_failure` has already run). `system` is a parameter
        because the prompt varies per turn (`/dagent` appends
        _DELEGATE_ONLY_SUFFIX).
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
        """`/workers`: the connected workers, and the names `/dagent` takes.

        Costs one `list_tools` round trip per worker so it never lists a
        machine that has gone down.
        """
        index = await ToolManager.build(
            self.clients,
            self.descriptions,
            reserved=_LOCAL_TOOL_NAMES,
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
        """Split a leading worker name off a `/dagent` request.

        `gpu-box render the scene` -> ("gpu-box", "render the scene") only when
        the first word is a configured worker; otherwise the whole string is
        the request.
        """
        head, _, rest = query.strip().partition(" ")
        if head in self.clients and rest.strip():
            return head, rest.strip()
        return None, query.strip()

    def resolve_worker_model_request(
        self, sub: str, arg: str
    ) -> tuple[str, dict[str, str] | None, str | None] | None:
        """Decide whether `/model <sub> <arg>` targets a connected worker, and
        what request that implies for its `model` tool.

        A worker name wins over a router subcommand of the same name. Returns
        None if `sub` is not a connected worker (the caller falls through to
        the router's own `/model`), (worker_id, None, error_text) for a worker
        with an invalid request (print the text, make no call), or (worker_id,
        arguments, None) with the dict to pass to `client.call_tool("model",
        ...)`.
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
        """Call a connected worker's `model` tool and format the result.

        Transport and protocol errors are caught and reported, never raised.
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

        `remote_only` drops the local tools from the request and `worker`
        narrows it to one machine's tools. Both withhold the schemas instead of
        instructing the model, because an absent tool is a fact and an
        instruction is a preference. Changing the tool list invalidates the
        cached prefix on this turn and on the next ordinary one.
        """
        final_text_response = ""
        processes.note_user_message(query)
        self.claude_service.add_user_message(self.messages, query)

        # Built once per user turn: the fleet cannot change mid-turn, and
        # rebuilding costs a `list_tools` round trip per worker per loop pass.
        index = await ToolManager.build(
            self.clients,
            self.descriptions,
            reserved=_LOCAL_TOOL_NAMES,
        )

        if remote_only:
            # The local half is withheld, not discouraged. `worker` narrows it
            # to one machine.
            tool_defs = (
                index.defs_for(worker) if worker else list(index.tool_defs)
            )
            system = SYSTEM_PROMPT + _DELEGATE_ONLY_SUFFIX

            # No worker tools and the local tools withheld would leave a turn
            # with no tools. Bail out and unwind the user message so the
            # aborted turn leaves no trace in self.messages.
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
            # Local tools first: they are static, so the front of the cached
            # prefix stays identical when a worker drops out.
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

        # Index of this turn's assistant message while a pause_turn
        # continuation is open. Each continuation replaces that slot instead of
        # appending, so assistant messages never stack without a user message
        # between them.
        pending_pause_turn_idx: int | None = None

        iterations = 0
        extra_continuations = 0
        # Set only when the next chat() call is required by the API: an open
        # pause_turn, or a server_tool_use left dangling by a mixed tool_use
        # response. Reset every pass, so the grace budget is not spent on
        # ordinary continuations.
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

            # end_turn, stop_sequence and max_tokens fall through to here. A
            # max_tokens cutoff mid-tool_use is not stop_reason "tool_use", so
            # nothing above answered that block; re-check the live message list
            # and finalize instead of returning as if the turn finished.
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
