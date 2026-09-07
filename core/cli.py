import asyncio
import json

from prompt_toolkit import PromptSession
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.styles import Style

from core import listen, speak
from core.chat import Chat


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
