"""Vector embeddings from your own private embedding server.

Anthropic has no first-party embeddings endpoint of its own; the documented
path is Voyage AI, a separate paid API. This module deliberately skips that
and instead calls whatever HTTP endpoint you already run for embeddings —
the same private compute an Unreal Engine project's own embedding settings
might already point at, or any self-hosted server speaking the increasingly
common OpenAI-compatible `/v1/embeddings` shape (text-embeddings-inference,
llama.cpp's server, Ollama, vLLM, LM Studio, ...).

It does nothing until `url` is set under `[embeddings]` in config.toml — the
tool is still declared to Claude either way (same pattern as `sql_query` when
`duckdb` isn't installed), so a fresh clone doesn't need a code change to make
it disappear, only the config comment left alone. Settings are re-read from
config.toml on every call rather than cached at import time, so uncommenting
a `url` (e.g. via the `config_edit` tool) takes effect on the next call
without restarting the app.

Requires:  pip install httpx        (already pulled in by `anthropic`, listed
                                      explicitly in requirements.txt for the
                                      direct import below)
"""

import asyncio
import json
import os
import tomllib
from pathlib import Path

TOOLS = [
    {
        "name": "text_embeddings",
        "description": (
            "Get vector embeddings for one or more strings from your own "
            "private embedding server, configured under [embeddings] in "
            "config.toml. There is no Anthropic-hosted embeddings endpoint — "
            "this deliberately bypasses Voyage AI so embeddings don't add a "
            "second paid API to the bill. Returns JSON with one vector per "
            "input string, in the same order. Does nothing until a url is "
            "set in config.toml; the error message says so and where to set it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {
                    "anyOf": [
                        {"type": "string"},
                        {"type": "array", "items": {"type": "string"}},
                    ],
                    "description": "A string, or a list of strings, to embed.",
                },
                "model": {
                    "type": "string",
                    "description": (
                        "Optional model name to send to the embedding server "
                        "for this call, overriding config.toml's "
                        "[embeddings].model. Most single-model private "
                        "servers ignore this field entirely."
                    ),
                },
            },
            "required": ["text"],
        },
    }
]

_TOOL_NAMES = {t["name"] for t in TOOLS}

# core/text_embeddings.py -> parent is core/, parent.parent is the repo root,
# same resolution main.py uses for CONFIG_PATH (it just starts from a
# different __file__).
_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.toml"


def handles(name: str) -> bool:
    return name in _TOOL_NAMES


async def execute(name: str, tool_input: dict) -> str:
    if name != "text_embeddings":
        return json.dumps({"error": f"unknown embeddings tool {name!r}"})
    return await asyncio.to_thread(_run, tool_input)


def _load_config() -> dict:
    try:
        with open(_CONFIG_PATH, "rb") as f:
            return tomllib.load(f).get("embeddings", {})
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as e:
        # Surfaced through _run's return, not raised, so a syntax error while
        # hand-editing config.toml shows up as a normal tool error rather than
        # an unhandled exception in the chat loop.
        raise ValueError(f"config.toml is not valid TOML: {e}") from e


def _run(tool_input: dict) -> str:
    try:
        config = _load_config()
    except ValueError as e:
        return json.dumps({"error": str(e)})

    url = config.get("url")
    if not url:
        return json.dumps(
            {
                "error": (
                    "no embedding server configured — uncomment and set "
                    "url under [embeddings] in config.toml, then try again"
                )
            }
        )

    text = tool_input.get("text")
    if not text:
        return json.dumps({"error": "no text provided"})
    inputs = [text] if isinstance(text, str) else list(text)
    if not inputs or not all(isinstance(t, str) for t in inputs):
        return json.dumps({"error": "'text' must be a string or a list of strings"})

    model = tool_input.get("model") or config.get("model")
    request_format = config.get("request_format", "openai")
    timeout = float(config.get("timeout", 30))

    try:
        import httpx
    except ImportError:
        return json.dumps(
            {
                "error": "httpx is not installed — `pip install httpx` to "
                "enable the text_embeddings tool (it normally ships already, "
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
                f"[text_embeddings] {api_key_env} is not set — calling "
                f"{url} without auth"
            )

    if request_format == "simple":
        payload: dict = {"text": inputs}
    else:  # "openai" — the de facto standard most self-hosted servers speak
        payload = {"input": inputs}
    if model:
        payload["model"] = model

    try:
        response = httpx.post(url, json=payload, headers=headers or None, timeout=timeout)
        response.raise_for_status()
    except httpx.HTTPError as e:
        return json.dumps({"error": f"request to {url} failed: {e}"})

    try:
        data = response.json()
    except ValueError as e:
        return json.dumps({"error": f"response from {url} was not valid JSON: {e}"})

    vectors = _extract_vectors(data)
    if vectors is None:
        shape = list(data) if isinstance(data, dict) else type(data).__name__
        return json.dumps(
            {
                "error": (
                    "could not find embedding vectors in the server's "
                    "response — expected OpenAI-style "
                    "{'data': [{'embedding': [...]}]} or "
                    "{'embeddings': [[...]]} / {'embedding': [...]}; "
                    f"got {shape}"
                )
            }
        )

    return json.dumps(
        {
            "model": model,
            "count": len(vectors),
            "dimensions": len(vectors[0]) if vectors else 0,
            "embeddings": vectors,
        }
    )


def _extract_vectors(data):
    """Accept a few common response shapes so one setting covers most
    self-hosted servers without a bespoke adapter per backend."""
    if isinstance(data, dict):
        if isinstance(data.get("data"), list):
            # OpenAI-compatible: {"data": [{"embedding": [...]}, ...]}
            try:
                return [item["embedding"] for item in data["data"]]
            except (KeyError, TypeError):
                return None
        if "embeddings" in data:
            return data["embeddings"]
        if "embedding" in data:
            return [data["embedding"]]
        return None
    if isinstance(data, list):
        return data
    return None
