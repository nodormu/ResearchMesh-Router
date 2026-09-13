import asyncio
import json

from prompt_toolkit import PromptSession
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.styles import Style

from core import listen, speak
from core.chat import Chat
from core.claude import load_claude_models, resolve_model_swap


class CliApp:
    def __init__(self, agent: Chat):
        self.agent = agent

        # Ported from ResearchMesh (see speak_listen_tool_integration_plan.md
        # in ResearchMesh's own /memories for the full design history) —
        # /voice and /listen below are otherwise identical to that repo's;
        # /workers and /dagent are the only genuinely router-specific
        # commands in this file. Off by default: this only controls whether
        # MY reply also gets spoken via the `speak` tool's own local `_run`
        # helper; it has no bearing on whether `speak`/`listen` are
        # reachable as Claude-invoked tools at all (that's config.toml's
        # own `[speak].enabled`).
        self.auto_speak = False

        self.history = InMemoryHistory()
        self.session: PromptSession[str] = PromptSession(
            history=self.history,
            style=Style.from_dict({"prompt": "#aaaaaa"}),
        )

    async def _submit(
        self,
        text: str,
        thinking: bool = False,
        remote_only: bool = False,
        worker: str | None = None,
    ):
        """Send `text` to the agent as one turn, print the reply, and speak
        it if `/voice` (auto_speak) is on. Shared by both a normal typed
        Enter-submit and a completed `/listen` dictation — this is what
        makes dictation auto-submit independent of the auto_speak flag:
        auto_speak only ever gates whether MY reply gets spoken, never
        whether YOUR input gets sent, regardless of which path (typed or
        dictated) produced that input. `thinking`/`remote_only`/`worker` are
        parsed by the caller from `/think`/`/dagent` before this is called —
        a dictated turn never carries either, since you can't speak a
        command prefix and dictation in the same breath, so it always goes
        through as a plain turn."""
        response = await self.agent.run(
            text, thinking=thinking, remote_only=remote_only, worker=worker
        )
        print(f"\nResponse:\n{response}")

        if self.auto_speak and response:
            # Off the event loop thread, same as every other local tool
            # call — speak.py's _run does blocking subprocess I/O (piper
            # synthesis, then paplay playback).
            result = json.loads(
                await asyncio.to_thread(speak._run, {"text": response})
            )
            if result.get("status") != "ok":
                print(
                    f"[voice: {result.get('status')} — "
                    f"{result.get('reason', result.get('error', ''))}]"
                )

    async def run(self):
        while True:
            try:
                user_input = await self.session.prompt_async("> ")
                if not user_input.strip():
                    continue

                text = user_input.strip()

                # `/workers` answers locally and never reaches the model — it is
                # a question about this process's state, not something to spend
                # a turn on.
                if text in ("/workers", "/workers "):
                    print(await self.agent.workers_listing())
                    continue

                # `/clear` is the recovery path from a history the API will no
                # longer accept — an unanswered tool_use block, or a
                # conversation past the context window. Both persist for the
                # life of the process, so without this the only way out is
                # killing the router and every worker connection with it.
                if text in ("/clear", "/reset"):
                    print(self.agent.clear())
                    continue

                # Toggle for whether my reply also gets spoken aloud, on top
                # of always being printed as text (never a replacement for
                # it). Reuses speak.py's own `_run` rather than
                # re-implementing synthesis/playback here.
                if text.startswith("/voice"):
                    arg = text[len("/voice"):].strip().lower()
                    if arg in ("on", "true", "1"):
                        self.auto_speak = True
                    elif arg in ("off", "false", "0"):
                        self.auto_speak = False
                    elif arg:
                        print(f"[voice: unrecognized arg {arg!r} — use /voice on|off]")
                        continue
                    print(f"[voice: {'on' if self.auto_speak else 'off'}]")
                    continue

                # Dictation: record+transcribe via listen.py's own `_run`,
                # then AUTO-SUBMIT the transcript as a turn the instant STT
                # completes — via the same `_submit` path a normal typed
                # Enter uses, so this happens regardless of whether `/voice`
                # (auto_speak) is on or off; that flag only affects whether
                # the REPLY gets spoken, never whether dictated input gets
                # sent. Optional `/listen <N>` overrides [listen]'s
                # configured duration for just this one call.
                if text.startswith("/listen"):
                    arg = text[len("/listen"):].strip()
                    tool_input = {}
                    if arg:
                        try:
                            tool_input["duration_seconds"] = int(arg)
                        except ValueError:
                            print(
                                f"[listen: bad duration {arg!r} — expected "
                                "an integer number of seconds]"
                            )
                            continue
                    print("[listening... speak now]")
                    result = json.loads(
                        await asyncio.to_thread(listen._run, tool_input)
                    )
                    if result.get("status") == "ok":
                        transcript = result["transcript"]
                        print(f"[dictated: {transcript!r}]")
                        await self._submit(transcript)
                    else:
                        print(
                            f"[listen: {result.get('status')} — "
                            f"{result.get('reason', result.get('error', ''))}]"
                        )
                    continue

                # /model lists config.toml's claude_models (re-read fresh
                # each call, see core/claude.py's load_claude_models — an
                # edit to config.toml shows up without a restart). /model
                # swap <name/index> actually changes it: session-only, it
                # never writes config.toml, so a new session always starts
                # back on claude_models[0]. An invalid name/index rejects
                # with an error and the valid list, same reject-don't-crash
                # pattern as /voice and /listen above. This (bare /model)
                # affects only the ROUTER's OWN reasoning model
                # (self.agent.claude_service) — see the worker-scoped branch
                # immediately below for changing a CONNECTED worker's model
                # instead.
                if text == "/model" or text.startswith("/model "):
                    rest = text[len("/model"):].strip()
                    parts = rest.split(None, 1)
                    sub = parts[0] if parts else ""
                    arg = parts[1].strip() if len(parts) > 1 else ""

                    # `/model <worker>` (list) or `/model <worker> swap
                    # <name/index>` — reaches into a CONNECTED worker's own
                    # `model` MCP tool directly (self.agent.clients), the
                    # same free/local/no-API-call pattern `/workers` already
                    # uses. Deliberately NOT reused via Chat.split_worker():
                    # that helper requires a non-empty remainder (built for
                    # /dagent, which always needs a task), but a bare
                    # `/model <worker>` legitimately has nothing after the
                    # worker name — this is its own worker-name check for
                    # exactly that reason. Every response line is prefixed
                    # `[worker: <name>] ` (the same tag format
                    # core/tools.py already uses for a worker's tool
                    # descriptions), so a remote result can never be mistaken
                    # for the router's own bare /model output above — never
                    # print an un-prefixed "[model: ...]" line for a worker
                    # result.
                    #
                    # The precedence check (`sub in self.agent.clients`) and
                    # the arg-parsing it implies are pulled into
                    # Chat.resolve_worker_model_request() (sync, pure — no MCP
                    # call), and the actual fallible MCP call + response
                    # formatting into Chat.call_worker_model() (async), so
                    # both are unit-testable without a live REPL or a real
                    # worker process — see smoke_test.py's
                    # check_model_worker_dispatch(). This branch is now just
                    # the same thin print/continue wrapper every other
                    # command here already is.
                    resolved = self.agent.resolve_worker_model_request(sub, arg)
                    if resolved is not None:
                        worker_id, arguments, error_text = resolved
                        if arguments is None:
                            print(error_text)
                        else:
                            print(
                                await self.agent.call_worker_model(
                                    worker_id, arguments
                                )
                            )
                        continue

                    try:
                        models = load_claude_models()
                    except ValueError as e:
                        print(f"[model: {e}]")
                        continue

                    if not sub:
                        current = self.agent.claude_service.model
                        lines = [
                            f"  {i}. {m}" + ("  (current)" if m == current else "")
                            for i, m in enumerate(models, start=1)
                        ]
                        print("[model: available]\n" + "\n".join(lines))
                        continue

                    if sub == "swap":
                        if not arg:
                            print("[usage: /model swap <name or index>]")
                            continue
                        chosen = resolve_model_swap(models, arg)
                        if chosen is None:
                            print(
                                f"[model: {arg!r} not recognized — "
                                "run /model to see the list]"
                            )
                            continue
                        self.agent.claude_service.model = chosen
                        print(f"[model: swapped to {chosen}]")
                        continue

                    print(
                        f"[model: unrecognized subcommand {sub!r} — "
                        "use /model or /model swap <name/index>]"
                    )
                    continue

                thinking = False
                if text.startswith("/think "):
                    text = text[len("/think "):]
                    thinking = True

                # `/dagent [worker] <task>` withholds the local tools for one
                # turn, optionally pinning to a single machine.
                remote_only = False
                worker = None
                if text.startswith("/dagent "):
                    remote_only = True
                    text = text[len("/dagent "):].strip()
                    worker, text = self.agent.split_worker(text)
                    if not text:
                        print("[usage: /dagent [worker] <task>]")
                        continue

                await self._submit(
                    text, thinking=thinking, remote_only=remote_only, worker=worker
                )

            except KeyboardInterrupt:
                break
            except Exception as e:
                # Chat.run() now resolves any pending tool_use blocks before
                # returning or raising (see core/chat.py), so self.messages
                # stays valid even after a bad turn — safe to report the
                # error and keep prompting instead of taking the whole
                # session down for what may be a single tool's failure.
                print(f"\n[error: {e}]")
