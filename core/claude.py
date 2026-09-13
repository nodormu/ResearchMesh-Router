import re
import tomllib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from anthropic import Anthropic
from anthropic.types import Message
from anthropic.types.beta import BetaMessage

from core.computer import BETA_FLAG as COMPUTER_BETA

# core/claude.py -> parent is core/, parent.parent is the repo root — same
# resolution main.py/core/vision.py/core/speak.py use for their own config
# path, so this doesn't drift if the repo is ever moved. Ported from
# ResearchMesh (see adding-model-command-to-swap-between-Anthropic-models.md
# in ResearchMesh's own /memories for the full design history) — this affects
# only the ROUTER's OWN reasoning model (self.claude_service below), never a
# connected worker's model.
_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.toml"


def load_claude_models() -> list[str]:
    """Read config.toml's [claude] claude_models array, fresh on every call —
    same "re-read, don't cache at import time" convention as core/vision.py,
    core/speak.py, core/text_embeddings.py. This is what lets /model (see
    core/cli.py) pick up a hand-edit to config.toml without an app restart.

    Falls back to a single-entry ["claude-sonnet-5"] list if the key is
    missing/empty/the file doesn't exist — matches main.py's own fallback for
    the same key, so a fresh clone still starts up fine before anyone has
    edited config.toml at all.
    """
    try:
        with open(_CONFIG_PATH, "rb") as f:
            models = tomllib.load(f).get("claude", {}).get("claude_models")
    except FileNotFoundError:
        models = None
    except tomllib.TOMLDecodeError as e:
        # Surfaced to the caller as a normal error string (see /model in
        # core/cli.py), not an unhandled exception in the chat loop — same
        # posture as vision.py/speak.py's identical re-raise.
        raise ValueError(f"config.toml is not valid TOML: {e}") from e
    return models or ["claude-sonnet-5"]


def resolve_model_swap(models: list[str], arg: str) -> str | None:
    """Match `arg` (from `/model swap <arg>`) against `models`.

    `arg` may be a 1-based index into the list (as shown by `/model`'s own
    listing) or the model name itself, matched case-insensitively since
    model ids are conventionally lowercase-hyphenated already — this only
    forgives typing, never introduces ambiguity beyond what plain case
    already would. Returns the canonical (as-configured) name, or None if
    `arg` matches nothing — the caller is responsible for the reject
    message, this function only ever returns a name from `models` or None,
    never raises, so a bad /model swap can't be a crash.
    """
    arg = arg.strip()
    if arg.isdigit():
        index = int(arg)
        if 1 <= index <= len(models):
            return models[index - 1]
        return None
    lowered = arg.lower()
    for model in models:
        if model.lower() == lowered:
            return model
    return None


# claude-sonnet-5, claude-opus-4-8, claude-haiku-4-5-20251001 -> "sonnet",
# "opus", "haiku". Whatever comes back from /v1/models, not a hardcoded list —
# the point of fetch_live_models() is to stop hand-typing model ids at all.
_FAMILY_RE = re.compile(r"^claude-([a-z]+)-")

# Anthropic's own docs recommend starting with Sonnet — the one deliberate,
# named exception to "pure recency, no hand-curation" below.
_PREFERRED_DEFAULT_FAMILY = "sonnet"

# Bounds worst-case startup latency and keeps a genuinely offline box (or CI's
# placeholder key) failing fast instead of hanging on DNS/connect.
_SCAN_TIMEOUT_SECONDS = 5.0

# How often refresh_claude_models() re-scans by default, if config.toml
# doesn't override it with its own [claude] model_scan_ttl_hours.
_DEFAULT_TTL_HOURS = 24


def fetch_live_models(client: Anthropic | None = None) -> list[str]:
    """Live-scan Anthropic's /v1/models and return one id per model family,
    newest-first by release date — except the sonnet family (if present) is
    always moved to the front, matching Anthropic's own documented default
    recommendation.

    Family = the alpha token right after "claude-" (sonnet/opus/haiku/...,
    whatever the API actually returns — nothing here hardcodes a family
    list). Raises whatever the SDK raises (auth error, timeout, connection
    error) on failure; callers decide the fallback, this function never
    guesses at one.
    """
    if client is None:
        client = Anthropic(timeout=_SCAN_TIMEOUT_SECONDS)

    newest_by_family: dict[str, tuple[str, datetime]] = {}
    for model in client.models.list():
        match = _FAMILY_RE.match(model.id)
        if not match:
            continue
        family = match.group(1)
        current = newest_by_family.get(family)
        if current is None or model.created_at > current[1]:
            newest_by_family[family] = (model.id, model.created_at)

    ordered = sorted(newest_by_family.items(), key=lambda kv: kv[1][1], reverse=True)
    ordered_ids = [model_id for _family, (model_id, _created_at) in ordered]

    preferred = newest_by_family.get(_PREFERRED_DEFAULT_FAMILY)
    if preferred is not None:
        preferred_id = preferred[0]
        ordered_ids.remove(preferred_id)
        ordered_ids.insert(0, preferred_id)

    return ordered_ids


def refresh_claude_models(
    *,
    force: bool = False,
    config_path: Path | None = None,
    fetch_fn: Callable[[], list[str]] | None = None,
) -> list[str]:
    """The actual /model data source: TTL-gated live scan with config.toml as
    a durable cache, never a hardcoded hand-typed array.

    - If config.toml's [claude] claude_models_checked_at is younger than
      model_scan_ttl_hours (default 24), skip the network call entirely and
      just return the cached claude_models array — most process starts hit
      this branch, no API call at all.
    - If the cache is stale (or `force=True`), attempt one live scan via
      fetch_fn (defaults to fetch_live_models). On success, claude_models AND
      claude_models_checked_at are updated together, atomically, in
      config.toml. On ANY failure (bad/placeholder key, offline, timeout,
      rate limit) nothing is written — config.toml is left exactly as it
      was, and the old cached array is returned as-is for this process.
      This is deliberate: it's what lets CI's smoke_test.py spawn
      mcp_server.py (ResearchMesh) — or, here, this repo's own smoke_test.py —
      with a placeholder API key and never have that touch config.toml or
      need real network access — it always just reads whatever was last
      committed.
    - Falls back to a single-entry ["claude-sonnet-5"] list only if
      config.toml itself is missing/unreadable AND there's nothing to scan
      with — the same last-resort default main.py has always had.

    config_path and fetch_fn exist purely for testability (same
    dependency-injection shape as fetch_live_models()'s own `client`
    parameter) — every real caller (main.py) uses the defaults, which are
    the real config.toml and the real live Anthropic scan. See
    smoke_test.py's check_model_refresh() for the fake-client-driven
    regression coverage this enables with no real network or file mutation.

    Reads use stdlib tomllib (same as load_claude_models() — no extra
    dependency, always available). tomlkit (comment-preserving write) is
    only imported lazily, right before the actual write, and only reached
    after a live scan has already succeeded — so a scan that fails (as it
    always does against CI's placeholder key) never even touches tomlkit,
    exactly like every other optional backing in this project.
    """
    config_path = config_path or _CONFIG_PATH
    fetch_fn = fetch_fn or fetch_live_models

    try:
        with open(config_path, "rb") as f:
            claude_section = tomllib.load(f).get("claude", {})
    except FileNotFoundError:
        claude_section = {}

    cached_models = list(claude_section.get("claude_models") or ["claude-sonnet-5"])
    ttl_hours = claude_section.get("model_scan_ttl_hours", _DEFAULT_TTL_HOURS)
    checked_at_raw = claude_section.get("claude_models_checked_at")

    if not force and checked_at_raw:
        try:
            checked_at = datetime.fromisoformat(str(checked_at_raw))
            if datetime.now(UTC) - checked_at < timedelta(hours=ttl_hours):
                return cached_models
        except ValueError:
            pass  # malformed timestamp — treat as stale, fall through to a scan

    try:
        fresh_models = fetch_fn()
    except Exception:
        # Network/auth/timeout — config.toml untouched, old cache stands.
        return cached_models

    try:
        import tomlkit
    except ImportError:
        # Scan succeeded but we can't persist it without tomlkit installed —
        # still return the fresh result for this process, just don't cache it.
        return fresh_models

    text = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    doc = tomlkit.parse(text) if text else tomlkit.document()
    if "claude" not in doc:
        doc["claude"] = tomlkit.table()
    # tomlkit's own stubs type doc["claude"] as `Item | Container`, which
    # mypy sees as not indexable — it is at runtime (this is tomlkit's own
    # documented usage pattern; verified correct by round-tripping a real
    # write/read against config.toml above). Narrow the type explicitly
    # rather than silence the whole line.
    claude_table = doc["claude"]
    assert isinstance(claude_table, tomlkit.items.Table)
    claude_table["claude_models"] = fresh_models
    claude_table["claude_models_checked_at"] = datetime.now(UTC).isoformat()

    scratch = config_path.with_name(config_path.name + ".tmp")
    scratch.write_text(tomlkit.dumps(doc), encoding="utf-8")
    scratch.replace(config_path)

    return fresh_models


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
