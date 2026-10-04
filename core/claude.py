import re
import tomllib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from anthropic import Anthropic, BadRequestError
from anthropic.types import Message
from anthropic.types.beta import BetaMessage

# core/claude.py -> parent is core/, parent.parent is the repo root: the same
# resolution main.py, core/vision.py and core/speak.py use for the config path.
# Affects only the router's own reasoning model, never a worker's.
_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.toml"


def load_claude_models() -> list[str]:
    """Read config.toml's [claude] claude_models array, fresh on every call so
    `/model` (core/cli.py) picks up a hand-edit without a restart.

    Falls back to ["claude-sonnet-5"] if the key or file is missing or empty,
    matching main.py.
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
    """Match `arg` from `/model swap <arg>` against `models`.

    `arg` is a 1-based index into the list `/model` shows, or a model name
    matched case-insensitively. Returns the name as configured, or None; never
    raises, so a bad swap cannot crash. The caller prints the rejection.
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
    """Live-scan /v1/models and return one id per model family, newest first by
    release date, with the sonnet family moved to the front.

    Family is the alpha token after "claude-"; no family list is hardcoded.
    Raises whatever the SDK raises on failure; the caller chooses the fallback.
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
    """The /model data source: a TTL-gated live scan with config.toml as the
    cache.

    - If claude_models_checked_at is younger than model_scan_ttl_hours (default
    24), return the cached claude_models array with no network call.
    - Otherwise (or with `force=True`) try one scan via fetch_fn. On success
    claude_models and claude_models_checked_at are written together. On any
    failure (placeholder key, offline, timeout, rate limit) nothing is written
    and the cached array is returned, so a smoke test with a placeholder key
    never touches config.toml.
    - Falls back to ["claude-sonnet-5"] only if config.toml is unreadable and
    there is nothing to scan with.

    `config_path` and `fetch_fn` exist for tests (see check_model_refresh in
    smoke_test.py). Reads use stdlib tomllib; tomlkit (comment-preserving
    write) is imported only just before a write that follows a successful scan.
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
        # Scan succeeded but tomlkit is missing: return the fresh result, do
        # not cache it.
        return fresh_models

    text = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    doc = tomlkit.parse(text) if text else tomlkit.document()
    if "claude" not in doc:
        doc["claude"] = tomlkit.table()
    # tomlkit's stubs type doc["claude"] as `Item | Container`, which mypy sees
    # as not indexable; it is at runtime. Narrow the type instead of silencing
    # the line.
    claude_table = doc["claude"]
    assert isinstance(claude_table, tomlkit.items.Table)
    claude_table["claude_models"] = fresh_models
    claude_table["claude_models_checked_at"] = datetime.now(UTC).isoformat()

    scratch = config_path.with_name(config_path.name + ".tmp")
    scratch.write_text(tomlkit.dumps(doc), encoding="utf-8")
    scratch.replace(config_path)

    return fresh_models


# Betas sent on every request. Empty: `computer_toolset_20260801` is a stable
# feature, so no header is needed. Requests still go to
# `client.beta.messages.create`, a superset of the stable endpoint.
#
# A worker's beta-gated tools are the worker's concern; it makes its own API
# call.
BETAS: list[str] = []

# The beta endpoint returns BetaMessage, which does not subclass Message, so
# the response-vs-raw-content checks must accept both. That is why
# `_RESPONSE_TYPES` exists: checking only `Message` would put the response
# object into `content` instead of its blocks.
_RESPONSE_TYPES = (Message, BetaMessage)


# Anthropic's fixed wording for "this model can't use one of the tool types you
# declared". Captures the comma-separated type list after the fixed phrase. The
# "Did you mean..." list that follows is not parsed: it names what is
# supported, and the retry loop discovers incompatibility empirically.
_UNSUPPORTED_TOOL_TYPES_RE = re.compile(r"does not support tool types: ([^.]+)\.")

# Bounds the retry-after-stripping loop in `chat()`. One BadRequestError names
# every offending type, so one retry normally suffices; the bound only guards
# against an API that reports them one at a time.
_MAX_UNSUPPORTED_TOOL_RETRIES = 5


class Claude:
    """Thin Anthropic SDK wrapper.

    Posts to `client.beta.messages.create` (see BETAS); top-level
    `cache_control` works on both endpoints, so prompt caching is unaffected.

    Per-model tool compatibility: a tool type the model rejects (e.g. Haiku 4.5
    and `computer_toolset_20260801`) fails the whole request, so after a
    `/model swap` every turn would 400 even without touching that tool.
    `_unsupported_by_model` remembers rejected types per model for the life of
    the process; `chat()` filters against it before each request and adds to it
    by parsing the "does not support tool types: ..." 400 the first time a
    pairing is tried. `/model swap` never clears it.
    """

    def __init__(self, model: str):
        self.client = Anthropic()
        self.model = model
        self._unsupported_by_model: dict[str, set[str]] = {}

    def _filter_unsupported(self, tools: list[dict] | None) -> list[dict] | None:
        """Drop any tool already known to be unsupported by `self.model`.

        Only ever touches Anthropic-defined tools (they carry a `type`
        field); a plain custom/JSON-schema local tool has no `type` key at
        all, so `t.get("type")` is `None` for those and this can never
        accidentally withhold one.
        """
        if not tools:
            return tools
        bad = self._unsupported_by_model.get(self.model)
        if not bad:
            return tools
        return [t for t in tools if t.get("type") not in bad]

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
        # No temperature, top_p or top_k: current models reject non-default
        # sampling parameters with a 400. Steer behaviour with the system
        # prompt.
        params = {
            "model": self.model,
            # Shared by adaptive thinking and the visible reply or tool_use.
            # 20000 keeps a single large tool call (a whole new file) from
            # being cut off by max_tokens mid-tool_use, and stays under the
            # SDK's ~21,333-token ceiling for non-streaming calls (above it
            # `messages.create` raises "Streaming is required"). Streaming is
            # not used: core/chat.py reads `response.content`, `stop_reason`
            # and `usage` as one static object.
            "max_tokens": 20000,
            "messages": messages,
            "betas": BETAS,
            # Prompt caching: top-level cache_control puts the breakpoint on
            # the last cacheable block, so each request re-reads the stable
            # prefix (tools, system, prior turns) at ~0.1x input price. Writes
            # cost ~1.25x, so it pays off from the second request; Chat's loop
            # makes up to MAX_TOOL_ITERATIONS per user turn.
            # A prefix under the model's minimum (1024 tokens on Sonnet 5) is
            # not cached, with no error. Any byte change early in the prefix
            # invalidates everything after it, so SYSTEM_PROMPT stays static
            # and the tool list stable; a worker dropping out reshapes the tool
            # list. Verify with CLAUDE_SHOW_USAGE=1.
            "cache_control": {"type": "ephemeral"},
        }

        # Adaptive thinking: the 4.5-era {"type": "enabled", "budget_tokens":
        # N} form returns a 400 on Sonnet 5 and Opus 5 / 4.7+, so there is no
        # thinking_budget. To bias depth use output_config={"effort":
        # "low"|"medium"|"high"|...}.
        if thinking:
            params["thinking"] = {"type": "adaptive"}

        if stop_sequences:
            params["stop_sequences"] = stop_sequences

        if tools:
            tools = self._filter_unsupported(tools)
            params["tools"] = tools

        if system:
            params["system"] = system

        # Beta endpoint, not client.messages.create (see BETAS).
        #
        # Retries here instead of raising for core/chat.py: an
        # unsupported-tool-type 400 is unrelated to the poisoned-history
        # failures `_call_chat_with_auto_repair` repairs, so chat.py never sees
        # it.
        last_error: BadRequestError | None = None
        for _ in range(_MAX_UNSUPPORTED_TOOL_RETRIES):
            try:
                return self.client.beta.messages.create(**params)
            except BadRequestError as e:
                last_error = e
                # `e.body` is the parsed error payload; prefer it to `str(e)`,
                # which wraps the same dict. Falls back to `str(e)` if the body
                # has an unexpected shape.
                body = getattr(e, "body", None)
                text = (
                    body.get("error", {}).get("message", "")
                    if isinstance(body, dict)
                    else ""
                ) or str(e)
                match = _UNSUPPORTED_TOOL_TYPES_RE.search(text)
                if not match or "tools" not in params:
                    raise
                newly_bad = {t.strip() for t in match.group(1).split(",") if t.strip()}
                still_present = newly_bad & {
                    t.get("type") for t in params["tools"] if t.get("type")
                }
                if not still_present:
                    # The error names a type already stripped or never in this
                    # request. Re-raise: retrying would loop on a message the
                    # regex matched for another cause.
                    raise
                self._unsupported_by_model.setdefault(self.model, set()).update(
                    still_present
                )
                params["tools"] = self._filter_unsupported(params["tools"])
                print(
                    f"[model compat] {self.model!r} does not support "
                    f"{sorted(still_present)} — withheld for the rest of "
                    "this session on this model, retrying this request..."
                )
        # Retry budget exhausted: raise the underlying error explicitly; a bare
        # `raise` has no active exception here and would raise a confusing
        # RuntimeError.
        assert last_error is not None
        raise last_error
