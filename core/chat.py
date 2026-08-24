import os
from collections.abc import Mapping

from anthropic.types import MessageParam, ToolResultBlockParam

from core import local_tools
from core.claude import Claude
from core.tools import ToolIndex, ToolManager, Worker

MAX_TOOL_ITERATIONS = 75

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
SYSTEM_PROMPT = """\
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
    """tool_use ids that never got a tool_result — the poisoned-session check.

    The API requires every tool_use block to be answered in the *immediately
    following* message. One that isn't doesn't just break the turn it happened
    in: the block stays in the history for the life of the process, so every
    later request fails the same way, however many turns later. That failure
    reads as "it started 400ing and won't stop", which is very hard to tell
    from a context overflow without looking.

    `_resolve_pending_tool_uses` exists to make this impossible. This is how you
    find out it didn't.
    """
    answered: set[str] = set()
    issued: list[str] = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            kind = _block_field(block, "type")
            if kind == "tool_use":
                block_id = _block_field(block, "id")
                if block_id:
                    issued.append(block_id)
            elif kind == "tool_result":
                used = _block_field(block, "tool_use_id")
                if used:
                    answered.add(used)
    return [i for i in issued if i not in answered]


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

    def _resolve_pending_tool_uses(self, response, reason: str) -> None:
        """Guarantee every tool_use block in `response` has a tool_result.

        self.messages persists for the life of the process (one Chat per
        run), so any tool_use left unresolved here doesn't just affect this
        turn — it poisons *every* request for the rest of the session with a
        400 (`tool_use ids were found without tool_result blocks immediately
        after`), because that block is still sitting there with nothing after
        it. This is the fallback of last resort for the two ways that used to
        happen: the MAX_TOOL_ITERATIONS cutoff firing right after a
        stop_reason == "tool_use" response (the loop broke before ever
        calling _run_tool_uses for it), and an exception escaping tool
        routing entirely. Safe to call even when there's nothing to resolve.
        """
        if response is None or response.stop_reason != "tool_use":
            return
        blocks = [b for b in response.content if b.type == "tool_use"]
        if not blocks:
            return
        results = [
            {
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": f"[{reason}]",
                "is_error": True,
            }
            for block in blocks
        ]
        self.claude_service.add_user_message(self.messages, results)

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

    def _report_api_failure(self, error: Exception) -> None:
        """Say which failure this is, rather than leaving it to guesswork.

        The two that persist look identical from the outside — the router
        starts 400ing and does not stop — but they have different causes and
        different fixes, and the error text plus these two numbers separate
        them every time.
        """
        text = str(error)
        count, chars = _approx_size(self.messages)
        orphans = _orphaned_tool_uses(self.messages)

        print(f"[api error] {text}")
        print(f"[api error] conversation: {count} messages, ~{chars:,} chars")

        if orphans:
            print(
                f"[api error] {len(orphans)} unanswered tool_use block(s): "
                f"{', '.join(orphans[:3])}"
                f"{' …' if len(orphans) > 3 else ''}"
            )
            print(
                "[api error] this poisons every later request in the session. "
                "Run /clear."
            )
        elif "too long" in text.lower() or "context" in text.lower():
            print(
                "[api error] the conversation has outgrown the context window. "
                "Run /clear."
            )

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

        response = None
        iterations = 0
        while True:
            iterations += 1
            if iterations > MAX_TOOL_ITERATIONS:
                if response is None:
                    # Only reachable with MAX_TOOL_ITERATIONS < 1, i.e. the
                    # limit was hit before anything was ever sent: there is no
                    # turn to resolve and no text to report.
                    break
                # `response` is still the last one we received (this iteration
                # never calls chat() again). If it ended on stop_reason ==
                # "tool_use", its tool_use blocks are already sitting in
                # self.messages with nothing after them — resolve them before
                # breaking, or the *next* user turn's first chat() call fails
                # immediately with a 400, however many messages later.
                self._resolve_pending_tool_uses(
                    response, "stopped: exceeded tool-iteration limit"
                )
                final_text_response = (
                    self.claude_service.text_from_message(response)
                    or "[stopped: exceeded tool-iteration limit]"
                )
                break

            try:
                response = self.claude_service.chat(
                    messages=self.messages,
                    system=system,
                    tools=tool_defs,
                    thinking=thinking,
                )
            except Exception as e:
                # Diagnose before returning. Both persistent failures leave the
                # history in a state where every later turn fails the same way,
                # so the useful information is *why*, and it is gone as soon as
                # this returns a bare error string.
                self._report_api_failure(e)
                return f"[api error: {e}]"
            if SHOW_USAGE:
                _report_usage(response)
            if thinking:
                thought = [b for b in response.content if b.type == "thinking"]
                print(f"[thinking blocks: {len(thought)}]")
            self.claude_service.add_assistant_message(self.messages, response)

            if response.stop_reason == "tool_use":
                print(self.claude_service.text_from_message(response))
                try:
                    tool_result_parts = await self._run_tool_uses(
                        response, index
                    )
                except Exception as e:
                    # execute_blocks already turns a per-block failure into an
                    # error tool_result rather than raising, so reaching here
                    # means something broke outside any single block's
                    # execution (routing itself). Resolve the pending blocks
                    # with a synthetic error result and stop for this turn
                    # instead of crashing the process and leaving self.messages
                    # permanently broken.
                    print(f"[tool routing error: {e}]")
                    self._resolve_pending_tool_uses(
                        response, f"tool execution failed: {e}"
                    )
                    final_text_response = f"[error running tools: {e}]"
                    break
                self.claude_service.add_user_message(
                    self.messages, tool_result_parts
                )
            elif response.stop_reason == "pause_turn":
                # A worker-side pause; resend the conversation to resume it.
                continue
            else:
                final_text_response = self.claude_service.text_from_message(
                    response
                )
                break

        return final_text_response
