from anthropic import Anthropic
from anthropic.types import Message


class Claude:
    """Thin Anthropic SDK wrapper.

    Deliberately simpler than ResearchMesh's copy, and for one reason: this
    client declares no tools of its own. ResearchMesh has to post to
    `client.beta.messages.create` with `betas=[computer_20251124]` because it
    declares the `computer` tool on every single request, and omitting that
    header 400s the whole conversation rather than just computer use. The router
    has no `computer` tool — no local tools at all — so there is nothing
    beta-gated to declare and the stable endpoint is correct.

    Dropping the beta endpoint also removes the subtlest trap in the original:
    it returns `BetaMessage`, which is *not* a subclass of `Message`, so the
    isinstance checks below needed a `_RESPONSE_TYPES` tuple covering both or
    they would silently stuff the response object into `content` instead of its
    blocks. With one response type that whole hazard is gone.

    (Top-level `cache_control` is available on the stable endpoint too — checked
    against the installed SDK, not assumed — so prompt caching is unaffected.)

    A worker's own beta-gated tools are entirely its problem: it makes its own
    API call, with its own headers, from its own machine. Nothing about a
    worker's tool schemas reaches this request.
    """

    def __init__(self, model: str):
        self.client = Anthropic()
        self.model = model

    def add_user_message(self, messages: list, message):
        user_message = {
            "role": "user",
            "content": message.content
            if isinstance(message, Message)
            else message,
        }
        messages.append(user_message)

    def add_assistant_message(self, messages: list, message):
        assistant_message = {
            "role": "assistant",
            "content": message.content
            if isinstance(message, Message)
            else message,
        }
        messages.append(assistant_message)

    def text_from_message(self, message: Message):
        return "\n".join(
            [block.text for block in message.content if block.type == "text"]
        )

    def chat(
        self,
        messages,
        system=None,
        stop_sequences=None,
        tools=None,
        thinking=False,
    ) -> Message:
        # No temperature / top_p / top_k. Current models (Sonnet 5, Opus 5, Opus
        # 4.7+) reject non-default sampling parameters with a 400, and the only
        # value they accept is the default — so sending it can never do anything
        # except fail. Steer behaviour with the system prompt instead.
        params = {
            "model": self.model,
            "max_tokens": 8000,
            "messages": messages,
            # Prompt caching. Top-level cache_control auto-places the breakpoint on
            # the last cacheable block, so each request re-reads the stable prefix
            # (tools -> system -> prior turns, in render order) at ~0.1x input price
            # instead of full. Writes cost ~1.25x, so it breaks even on the second
            # request — and Chat's agentic loop makes up to MAX_TOOL_ITERATIONS
            # requests per user turn, each resending the whole conversation.
            #
            # Silent-failure notes: a prefix under the model's minimum (1024 tokens
            # on Sonnet 5) simply isn't cached, with no error. And any byte change
            # early in the prefix invalidates everything after it — so keep
            # SYSTEM_PROMPT static and the tool list in a stable order. That second
            # point is sharper here than in ResearchMesh: the tool list is built
            # from live workers, so a worker dropping out mid-session reshapes the
            # prefix and costs the cache. Verify with CLAUDE_SHOW_USAGE=1.
            "cache_control": {"type": "ephemeral"},
        }

        # Adaptive thinking replaces the old fixed budget. The 4.5-era form
        # {"type": "enabled", "budget_tokens": N} now returns a 400 on Sonnet 5
        # and Opus 5 / 4.7+, so there is no thinking_budget to pass — Claude
        # decides how much to think per request. If you ever want to bias that,
        # the knob is output_config={"effort": "low"|"medium"|"high"|...}, which
        # controls depth rather than a token count.
        if thinking:
            params["thinking"] = {"type": "adaptive"}

        if stop_sequences:
            params["stop_sequences"] = stop_sequences

        if tools:
            params["tools"] = tools

        if system:
            params["system"] = system

        message = self.client.messages.create(**params)
        return message
