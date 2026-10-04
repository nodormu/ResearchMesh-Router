"""Speak text aloud through a local text-to-speech engine (Piper) and play it
through the configured audio output.

Self-hosted and config-driven like `vision.py` and `text_embeddings.py`; it
does nothing until `config.toml` has `[speak]` set up, and the tool is declared
to Claude either way. It can decline to speak for two reasons, returned as a
`status` field, not raised:
  - "disabled": `[speak].enabled` is false. Checked first, so a disabled tool
never touches the filesystem or spawns a subprocess.
  - "not_configured": `[speak].voice_model` is unset or the file it points at
does not exist.

Settings are re-read from config.toml on every call.

Requires:  pip install piper-tts (NOT `sudo apt install piper`, an unrelated
GTK app; the engine is the PyPI package, run as `python3 -m piper`)
           A Piper voice model (.onnx plus its .onnx.json sidecar), not fetched
by this file.
"""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

TOOLS = [
    {
        "name": "speak",
        "description": (
            "Speak text aloud through your own local text-to-speech engine "
            "(Piper) and play it through your configured audio output. "
            "Configured under [speak] in config.toml. If speak is disabled "
            "in config, or no voice model is configured/installed, this "
            "returns a {'status': 'disabled' | 'not_configured', ...} "
            "result and does NOT fall back to any other TTS or to text-only "
            "output — tell the user what happened and wait for guidance "
            "rather than silently trying something else."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "The text to speak aloud.",
                },
                "voice": {
                    "type": "string",
                    "description": (
                        "Not required — a path to a different Piper .onnx voice "
                        "model for this call, overriding config.toml's "
                        "[speak].voice_model. Must have a matching "
                        "<path>.json sidecar file, same as the default."
                    ),
                },
            },
            "required": ["text"],
        },
    }
]

_TOOL_NAMES = {t["name"] for t in TOOLS}

# core/speak.py -> parent is core/, parent.parent is the repo root, same
# resolution vision.py/text_embeddings.py use for their config path.
_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.toml"


def handles(name: str) -> bool:
    return name in _TOOL_NAMES


async def execute(name: str, tool_input: dict) -> str:
    if name != "speak":
        return json.dumps({"error": f"unknown speak tool {name!r}"})
    return await asyncio.to_thread(_run, tool_input)


def _load_config() -> dict:
    try:
        with open(_CONFIG_PATH, "rb") as f:
            return tomllib.load(f).get("speak", {})
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as e:
        # Surfaced through _run's return, not raised, so a syntax error
        # while hand-editing config.toml shows up as a normal tool error
        # rather than an unhandled exception in the chat loop.
        raise ValueError(f"config.toml is not valid TOML: {e}") from e


def _run(tool_input: dict) -> str:
    try:
        config = _load_config()
    except ValueError as e:
        return json.dumps({"error": str(e)})

    text = tool_input.get("text")
    if not text:
        return json.dumps({"error": "'text' is required"})

    # 1. `enabled` first, before touching anything else — a disabled tool
    #    should have zero filesystem/subprocess side effects.
    if not config.get("enabled", True):
        return json.dumps(
            {
                "status": "disabled",
                "reason": "speak is disabled — set [speak].enabled = true "
                "in config.toml to turn it back on",
            }
        )

    # 2. Voice model configured and actually present on disk.
    voice_model = tool_input.get("voice") or config.get("voice_model")
    if not voice_model:
        return json.dumps(
            {
                "status": "not_configured",
                "reason": (
                    "no voice model configured — set [speak].voice_model "
                    "in config.toml to a Piper .onnx voice file"
                ),
            }
        )
    voice_path = Path(voice_model).expanduser()
    if not voice_path.is_file():
        return json.dumps(
            {
                "status": "not_configured",
                "reason": f"voice model not found: {voice_path}",
            }
        )

    timeout = float(config.get("timeout", 30))
    sink = config.get("sink")

    # 3. Synthesize to a temp WAV file via the piper-tts PyPI package,
    # invoked as `python3 -m piper` (NOT the apt `piper` binary — see the
    # naming-collision warning in this module's docstring).
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        wav_path = tmp.name

    try:
        synth = subprocess.run(
            [
                sys.executable, "-m", "piper",
                "-m", str(voice_path),
                "-f", wav_path,
            ],
            input=text,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if synth.returncode != 0:
            return json.dumps(
                {
                    "status": "error",
                    "reason": f"synthesis failed: {synth.stderr.strip()}",
                }
            )

        # 4. Play it — block until playback finishes, since a "speak" tool
        # call is expected to have actually finished speaking before the
        # tool result returns to the conversation.
        play_cmd = ["paplay"]
        if sink:
            play_cmd.append(f"--device={sink}")
        play_cmd.append(wav_path)

        playback = subprocess.run(
            play_cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
        if playback.returncode != 0:
            return json.dumps(
                {
                    "status": "error",
                    "reason": f"playback failed: {playback.stderr.strip()}",
                }
            )
    except subprocess.TimeoutExpired as e:
        return json.dumps(
            {"status": "error", "reason": f"timed out after {timeout}s: {e}"}
        )
    finally:
        # Not `trash` — this is a synthesized scratch file, not user data,
        # so an ordinary remove (rather than the recoverable-trash
        # convention used for user-facing deletes elsewhere) is appropriate.
        try:
            os.remove(wav_path)
        except OSError:
            pass

    return json.dumps({"status": "ok", "text": text})
