"""Ask a question about an image, using your own private vision-capable
model server.

Same motivation and shape as `text_embeddings.py`: this deliberately calls a
self-hosted OpenAI-compatible `/v1/chat/completions` endpoint (llama.cpp's
server, vLLM, LM Studio, ...) instead of Claude's own vision, so images can
stay on your own local/private compute instead of round-tripping through
Anthropic's API.

It does nothing until `url` is set under `[vision]` in config.toml — the
tool is still declared to Claude either way (same "disappears gracefully on
a fresh clone" pattern as `text_embeddings`/`sql_query`). Settings are
re-read from config.toml on every call rather than cached at import time, so
editing `url`/`model`/`max_tokens`/`timeout` takes effect on the next call
with no restart.

IMPORTANT — no automatic fallback to Claude's own vision lives in this file.
If the local server isn't configured or isn't reachable, `_run` returns a
plain `{"status": "local_unavailable", ...}` result and stops there. That is
intentional: whether to then look at the same image using Claude's own
native vision is a conversation-level decision, made only after telling the
user the local server is down and getting explicit confirmation — never
silently, since that would send an image to Anthropic's API that the user
may have specifically wanted kept local. See the tool description below,
which states this expectation directly so it holds even in a fresh session
that has never read any planning notes about this tool.

Requires:  pip install httpx        (already pulled in by `anthropic`, listed
                                      explicitly in requirements.txt for the
                                      direct import below)
"""

import asyncio
import base64
import json
import mimetypes
import os
import tomllib
from pathlib import Path

TOOLS = [
    {
        "name": "vision_query",
        "description": (
            "Ask a question about an image using your own private "
            "vision-capable model server, configured under [vision] in "
            "config.toml. Same self-hosting motivation as text_embeddings: "
            "keeps images on your own local/private compute instead of "
            "sending them to Anthropic's API. `image` accepts a local file "
            "path, an http(s) URL, or a data: URI. If the local server "
            "isn't configured or isn't reachable, this returns a "
            "{'status': 'local_unavailable', ...} result and does NOT "
            "automatically fall back to anything else — tell the user the "
            "local server is unavailable and get their explicit "
            "confirmation before using your own vision to look at the "
            "image instead (e.g. via the file editor tool's image-viewing "
            "support for a local file), since that means sending it to "
            "Anthropic's API rather than keeping it local. Never do that "
            "fallback silently."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "image": {
                    "type": "string",
                    "description": (
                        "A local file path, an http(s) URL, or a data: URI "
                        "pointing at the image to ask about."
                    ),
                },
                "prompt": {
                    "type": "string",
                    "description": (
                        "The question or instruction about the image, e.g. "
                        "'List the characters visible on this cover and "
                        "describe the art style.'"
                    ),
                },
                "model": {
                    "type": "string",
                    "description": (
                        "Optional model name to send to the vision server "
                        "for this call, overriding config.toml's "
                        "[vision].model."
                    ),
                },
                "max_tokens": {
                    "type": "integer",
                    "description": (
                        "Optional max output tokens for this call, "
                        "overriding config.toml's [vision].max_tokens "
                        "(default 4000 — GLM-4.6V-Flash is a reasoning "
                        "model and spends part of this budget on "
                        "invisible reasoning before the visible answer, "
                        "confirmed empirically to need well above 512 "
                        "for a moderately complex image)."
                    ),
                },
            },
            "required": ["image", "prompt"],
        },
    }
]

_TOOL_NAMES = {t["name"] for t in TOOLS}

# core/vision.py -> parent is core/, parent.parent is the repo root, same
# resolution main.py/text_embeddings.py use for their config path.
_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.toml"


def handles(name: str) -> bool:
    return name in _TOOL_NAMES


async def execute(name: str, tool_input: dict) -> str:
    if name != "vision_query":
        return json.dumps({"error": f"unknown vision tool {name!r}"})
    return await asyncio.to_thread(_run, tool_input)


def _load_config() -> dict:
    try:
        with open(_CONFIG_PATH, "rb") as f:
            return tomllib.load(f).get("vision", {})
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as e:
        # Surfaced through _run's return, not raised, so a syntax error
        # while hand-editing config.toml shows up as a normal tool error
        # rather than an unhandled exception in the chat loop.
        raise ValueError(f"config.toml is not valid TOML: {e}") from e


def _resolve_image(image: str) -> tuple[str, str | tuple[str, str]] | None:
    """Classify an `image` argument and prepare it for the request payload.

    Returns (kind, value):
      - ("url", <the original string>) for http(s)/data: inputs — passed
        straight through as-is, since llama.cpp's `image_url.url` accepts
        remote URLs and data URIs natively.
      - ("b64", (media_type, base64_data)) for a local file — read and
        encoded here, so it works regardless of whether the target server
        was started with `--media-path` (this one wasn't).
    Returns None if a local path doesn't exist.
    """
    if image.startswith(("http://", "https://", "data:")):
        return ("url", image)
    path = Path(image).expanduser()
    if not path.is_file():
        return None
    media_type = mimetypes.guess_type(str(path))[0] or "image/png"
    b64 = base64.b64encode(path.read_bytes()).decode()
    return ("b64", (media_type, b64))


def _run(tool_input: dict) -> str:
    try:
        config = _load_config()
    except ValueError as e:
        return json.dumps({"error": str(e)})

    image = tool_input.get("image")
    prompt = tool_input.get("prompt")
    if not image or not prompt:
        return json.dumps({"error": "both 'image' and 'prompt' are required"})

    resolved = _resolve_image(image)
    if resolved is None:
        return json.dumps({"error": f"image not found: {image!r}"})

    url = config.get("url")
    if not url:
        return json.dumps(
            {
                "status": "local_unavailable",
                "reason": (
                    "no vision server configured — uncomment and set url "
                    "under [vision] in config.toml"
                ),
                "image": image,
                "prompt": prompt,
            }
        )

    model = tool_input.get("model") or config.get("model")
    max_tokens = tool_input.get("max_tokens") or config.get("max_tokens", 4000)
    # 180s, not 120s: a real test at max_tokens=4000 used 1536 tokens in 43s
    # (~35.7 tok/s combined reasoning+generation on this rig) — a call that
    # actually used the full 4000-token budget would extrapolate to ~110s,
    # too close to a 120s ceiling for comfort. 180s leaves real headroom.
    timeout = float(config.get("timeout", 180))

    kind, value = resolved
    image_url = value if kind == "url" else f"data:{value[0]};base64,{value[1]}"

    payload: dict = {
        "max_tokens": max_tokens,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            }
        ],
    }
    if model:
        payload["model"] = model

    try:
        import httpx
    except ImportError:
        return json.dumps(
            {
                "error": "httpx is not installed — `pip install httpx` to "
                "enable the vision_query tool (it normally ships already, "
                "pulled in by the `anthropic` package)"
            }
        )

    headers = {}
    api_key_env = config.get("api_key_env")
    if api_key_env:
        token = os.getenv(api_key_env)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        else:
            print(
                f"[vision_query] {api_key_env} is not set — calling "
                f"{url} without auth"
            )

    try:
        response = httpx.post(url, json=payload, headers=headers or None, timeout=timeout)
        response.raise_for_status()
    except httpx.HTTPError as e:
        # Local server down/unreachable -> STOP here, do not auto-retry
        # anywhere (see module docstring). Report it plainly; the outer
        # chat loop is expected to tell the user and wait for explicit
        # go-ahead before using its own vision on this image.
        return json.dumps(
            {
                "status": "local_unavailable",
                "reason": f"request to {url} failed: {e}",
                "image": image,
                "prompt": prompt,
            }
        )

    try:
        data = response.json()
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, ValueError) as e:
        return json.dumps(
            {"error": f"unexpected response shape from {url}: {e}"}
        )

    return json.dumps({"source": "local", "model": model, "text": text})
