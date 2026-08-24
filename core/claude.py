from anthropic import Anthropic
from anthropic.types import Message
from anthropic.types.beta import BetaMessage

from core.computer import BETA_FLAG as COMPUTER_BETA

# Betas sent on every request. The `computer` tool's `computer_20251124` schema
# is beta-gated and local_tools declares it unconditionally, so this header must
# be unconditional too — omitting it 400s the whole request, not just computer
# use. The beta Messages endpoint is a superset of the stable one, so nothing
# else changes shape.
#
# This is a straight revert of a simplification that held only while the router
# had no local tools of its own. A *worker's* beta-gated tools remain entirely
# the worker's problem — it makes its own API call with its own headers, and
# nothing about a worker's schemas reaches this request. What changed is that
# the router now declares `computer` itself.
BETAS = [COMPUTER_BETA]

# The beta endpoint returns BetaMessage, which is NOT a subclass of Message, so
# the response-vs-raw-content checks below must accept both. Testing only
# `Message` would silently stuff the response object into `content` instead of
# its blocks — the subtlest trap in this file, and the reason `_RESPONSE_TYPES`
# exists rather than a bare isinstance.
_RESPONSE_TYPES = (Message, BetaMessage)


class Claude:
    """Thin Anthropic SDK wrapper.

    Posts to `client.beta.messages.create` because the local `computer` tool is
    beta-gated; see BETAS above. Top-level `cache_control` works on both
    endpoints, so prompt caching is unaffected by the switch.
    """

    def __init__(self, model: str):
        self.client = Anthropic()
        self.model = model

    def add_user_message(self, messages: list, message):
        user_message = {
            "role": "user",
            "content": message.content
            if isinstance(message, _RESPONSE_TYPES)
            else message,
        }
        messages.append(user_message)

    def add_assistant_message(self, messages: list, message):
        assistant_message = {
            "role": "assistant",
            "content": message.content
            if isinstance(message, _RESPONSE_TYPES)
            else message,
        }
        messages.append(assistant_message)

    def text_from_message(self, message: Message | BetaMessage):
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
    ) -> BetaMessage:
        # No temperature / top_p / top_k. Current models (Sonnet 5, Opus 5, Opus
        # 4.7+) reject non-default sampling parameters with a 400, and the only
        # value they accept is the default — so sending it can never do anything
        # except fail. Steer behaviour with the system prompt instead.
        params = {
            "model": self.model,
            "max_tokens": 8000,
            "messages": messages,
            "betas": BETAS,
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

        # Beta endpoint, not client.messages.create — see BETAS above.
        message = self.client.beta.messages.create(**params)
        return message
