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

                text = user_input
                thinking = False
                if text.startswith("/think "):
                    text = text[len("/think "):]
                    thinking = True

                response = await self.agent.run(text, thinking=thinking)
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
