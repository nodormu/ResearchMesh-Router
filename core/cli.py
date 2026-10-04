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

        # /voice and /listen are shared with ResearchMesh; /workers and /dagent
        # are router-specific. Off by default: it only controls whether replies
        # are also spoken through the `speak` tool's `_run`, not whether
        # `speak` and `listen` are reachable as tools (that is
        # `[speak].enabled`).
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
        """Send `text` to the agent as one turn, print the reply, and speak it
        if `/voice` (auto_speak) is on.

        Shared by typed input and a finished `/listen` dictation, so auto_speak
        only gates speaking the reply, never sending the input. `thinking`,
        `remote_only` and `worker` come from `/think` and `/dagent` in the
        caller; a dictated turn never carries them.
        """
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

                # `/clear` is the way out of a history the API no longer
                # accepts: an unanswered tool_use block or a conversation past
                # the context window. Both persist for the life of the process;
                # the alternative is killing the router and every worker
                # connection.
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

                # Dictation: record and transcribe via listen.py's `_run`, then
                # auto-submit the transcript through the same `_submit` path as
                # typed input, whatever `/voice` is set to. `/listen <N>`
                # overrides [listen]'s duration for this call.
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

                # `/model` lists config.toml's claude_models, re-read on every
                # call (core/claude.py `load_claude_models`), so an edit shows
                # up without a restart. `/model swap <name/index>` changes it
                # for the session only and never writes config.toml; a new
                # session starts on claude_models[0]. An invalid name or index
                # is rejected with the valid list. This bare form affects only
                # the router's own reasoning model
                # (`self.agent.claude_service`); the worker branch below
                # changes a connected worker's model.
                if text == "/model" or text.startswith("/model "):
                    rest = text[len("/model"):].strip()
                    parts = rest.split(None, 1)
                    sub = parts[0] if parts else ""
                    arg = parts[1].strip() if len(parts) > 1 else ""

                    # `/model <worker>` (list) and `/model <worker> swap
                    # <name/index>` call a connected worker's own `model` MCP
                    # tool directly (`self.agent.clients`), like `/workers`: no
                    # API call. Not built on Chat.split_worker(), which
                    # requires a non-empty remainder (for /dagent); a bare
                    # `/model <worker>` has none. Every response line is
                    # prefixed `[worker: <name>] ` (the tag core/tools.py uses
                    # in worker tool descriptions), so a worker's result is
                    # never mistaken for the router's own `/model` output.
                    # The precedence check (`sub in self.agent.clients`) and
                    # argument parsing are in
                    # Chat.resolve_worker_model_request() (pure), and the MCP
                    # call and formatting in Chat.call_worker_model() (async),
                    # so both are testable without a REPL; see
                    # check_model_worker_dispatch() in smoke_test.py.
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
                # turn, and may pin to a single machine too.
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
