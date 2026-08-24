from prompt_toolkit import PromptSession
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.styles import Style

from core.chat import Chat


class CliApp:
    def __init__(self, agent: Chat):
        self.agent = agent

        self.history = InMemoryHistory()
        self.session: PromptSession[str] = PromptSession(
            history=self.history,
            style=Style.from_dict({"prompt": "#aaaaaa"}),
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

                response = await self.agent.run(
                    text,
                    thinking=thinking,
                    remote_only=remote_only,
                    worker=worker,
                )
                print(f"\nResponse:\n{response}")

            except KeyboardInterrupt:
                break
            except Exception as e:
                # Chat.run() now resolves any pending tool_use blocks before
                # returning or raising (see core/chat.py), so self.messages
                # stays valid even after a bad turn — safe to report the
                # error and keep prompting instead of taking the whole
                # session down for what may be a single tool's failure.
                print(f"\n[error: {e}]")
