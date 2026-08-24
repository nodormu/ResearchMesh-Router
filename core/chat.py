import os

from anthropic.types import MessageParam

from core.claude import Claude
from core.tools import ToolIndex, ToolManager
from mcp_client import MCPClient

MAX_TOOL_ITERATIONS = 75

# Set CLAUDE_SHOW_USAGE=1 to print token and cache counters per request. Prompt
# caching fails *silently* (a too-short prefix or a changed byte early in the
# prefix just means no hit, with no error), so this is the only way to confirm
# the cache_control breakpoint in core/claude.py is actually paying off.
SHOW_USAGE = os.getenv("CLAUDE_SHOW_USAGE") == "1"

# Sent as the `system` parameter on every request.
#
# ResearchMesh's version of this prompt exists to stop Claude inventing local
# capabilities it doesn't have. This one has the opposite problem: every
# capability is real but lives on a *different machine*, and nothing in a tool
# schema conveys that. Left to the schemas alone the model treats the fleet as
# one computer — it reads a path from one worker and writes it on another, or
# assumes the box that has a GPU also has the browser open.
#
# Everything here is a fact about this topology that Claude cannot infer.
SYSTEM_PROMPT = """\
You are the router in a command-line client that orchestrates a fleet of remote
worker agents. You have no tools of your own — no shell, no filesystem, no browser,
no Python. You cannot read a file or run a command yourself. Every tool in your list
belongs to a worker on another machine, and calling it is the only way anything
happens.

Tool names are `<worker>__<tool>`. The part before the double underscore is the
worker the call runs on, and each tool's description opens with `[worker: name]`
followed by what that machine is for. Read that header before choosing — it is the
only thing distinguishing two workers that expose identically-named tools.

Workers are separate machines. They do not share a filesystem, a network view, a
clipboard, or any state. A path, a running process, a database file, or a browser
page on one worker does not exist on another. To move data between workers you must
read it out of one and pass it into the other as part of the task text. Never assume
a worker can see something another worker produced.

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
unrelated conversations.

Running work in parallel: issuing several tool calls in one turn makes different
workers run at the same time, which is the main reason this fleet exists. Two calls
to the *same* worker do not overlap — a ResearchMesh worker has one mouse, one
browser page and one kernel, and serialises its delegations — so batch across
workers, not within one. Split genuinely independent work; keep dependent steps in
order.

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


class Chat:
    def __init__(
        self,
        claude_service: Claude,
        clients: dict[str, MCPClient],
        descriptions: dict[str, str] | None = None,
        max_parallel: int = 8,
    ):
        self.claude_service: Claude = claude_service
        self.clients: dict[str, MCPClient] = clients
        # worker id -> the routing blurb from its config.toml entry
        self.descriptions: dict[str, str] = descriptions or {}
        self.max_parallel: int = max_parallel
        self.messages: list[MessageParam] = []
        self._announced = False

    async def _run_tool_uses(self, message, index: ToolIndex) -> list:
        """Hand every tool_use block to ToolManager, which fans them out.

        Every tool_use block owes the API a matching tool_result in the very
        next message, no exceptions. `execute_blocks` guarantees one result per
        block — including for a worker that died mid-call — so unlike
        ResearchMesh's version there is no local-executor branch here that could
        raise and orphan the blocks after it.
        """
        blocks = [b for b in message.content if b.type == "tool_use"]
        return await ToolManager.execute_blocks(
            index, blocks, max_parallel=self.max_parallel
        )

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

    async def run(self, query: str, thinking: bool = False) -> str:
        final_text_response = ""
        self.claude_service.add_user_message(self.messages, query)

        # Built once per user turn, not once per tool-use iteration. The fleet
        # can't change mid-turn, and re-deriving it was a `list_tools` round
        # trip per worker per loop pass — over the network, up to
        # MAX_TOOL_ITERATIONS times for a single question.
        index = await ToolManager.build(self.clients, self.descriptions)
        tool_defs = index.tool_defs

        if not self._announced:
            print(f"[router] {index.summary()}")
            self._announced = True
        if not tool_defs:
            print(
                "[router] no worker tools available — answering without the fleet"
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

            response = self.claude_service.chat(
                messages=self.messages,
                system=SYSTEM_PROMPT,
                tools=tool_defs,
                thinking=thinking,
            )
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
