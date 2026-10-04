"""Record audio from the configured microphone for a bounded window and
transcribe it locally with faster-whisper (CPU, no cloud STT).

Self-hosted and config-driven like `speak.py`, `vision.py` and
`text_embeddings.py`; it does nothing until `config.toml` has `[listen]` set
up. It can decline to record for two reasons, returned as a `status` field, not
raised:
  - "disabled": `[listen].enabled` is false. Checked before `device`, so the
microphone is never opened.
  - "not_configured": `[listen].device` is unset.

Settings are re-read from config.toml on every call.

Capture is one subprocess (`timeout <N> parecord ...`) and transcription is one
in-process call (`WhisperModel(...).transcribe(...)`): faster-whisper has no
CLI, unlike Piper in `speak.py`.

`timeout <N> parecord` exits 124 when it cuts the recording off after N
seconds. That is the normal success case; the WAV is written correctly. Only
other non-zero exit codes (device busy, bad device name) are capture failures.

Requires:  pip install faster-whisper (CPU/int8, no GPU needed)
           A PipeWire microphone source; `pactl list sources short` lists
devices, and mic input volume matters a lot for accuracy.
"""

import asyncio
import json
import os
import subprocess
import tempfile
import tomllib
from pathlib import Path

TOOLS = [
    {
        "name": "listen",
        "description": (
            "Record audio from your own configured microphone for a "
            "bounded window and transcribe it locally via faster-whisper "
            "(CPU, no cloud STT). Configured under [listen] in "
            "config.toml. If listen is disabled in config, or no "
            "microphone device is configured, this returns a "
            "{'status': 'disabled' | 'not_configured', ...} result — tell "
            "the user what happened rather than assuming a transcript "
            "exists."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "duration_seconds": {
                    "type": "integer",
                    "description": (
                        "How many seconds to record. Defaults to "
                        "[listen].default_duration_seconds if unset "
                        "(typically 8-10s). Clamped to "
                        "[listen].max_duration_seconds as a safety cap "
                        "regardless of what's requested here."
                    ),
                },
                "device": {
                    "type": "string",
                    "description": (
                        "Not required — a PipeWire source name to override "
                        "[listen].device for this call."
                    ),
                },
            },
            "required": [],
        },
    }
]

_TOOL_NAMES = {t["name"] for t in TOOLS}

# core/listen.py -> parent is core/, parent.parent is the repo root, same
# resolution speak.py/vision.py use for their config path.
_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.toml"


def handles(name: str) -> bool:
    return name in _TOOL_NAMES


# `model.transcribe()` returns a lazy generator: per-segment decoding happens
# when the caller iterates it, so the timeout must cover the iteration as well
# as the call. faster-whisper has no subprocess to kill, so `asyncio.wait_for`
# bounds only the caller's wait, not the thread.
# Budget: about 2.6s for a 30s clip (~12x real time) on this machine; `max(60,
# duration*4)` leaves margin for a slower CPU or a larger model_size.
_TRANSCRIBE_TIMEOUT_MIN = 60
_TRANSCRIBE_TIMEOUT_PER_SECOND = 4
# Same role as core/midi1.py's _POLL_TIMEOUT_MARGIN: extra headroom so this
# outer bound can't race the capture step's own inner timeout (duration+5)
# and win by a hair, reporting a generic "hung" message for what was
# actually just a well-behaved capture running right up to its own limit.
_OUTER_TIMEOUT_MARGIN = 5


def _resolve_duration(tool_input: dict, config: dict) -> int:
    """Shared by execute() (to size the outer timeout) and _run() (to size
    the actual capture) — kept as one function so the two can't drift."""
    duration = tool_input.get("duration_seconds") or config.get(
        "default_duration_seconds", 8
    )
    max_duration = config.get("max_duration_seconds", 30)
    return max(1, min(int(duration), int(max_duration)))


async def execute(name: str, tool_input: dict) -> str:
    if name != "listen":
        return json.dumps({"error": f"unknown listen tool {name!r}"})

    try:
        config = _load_config()
        duration = _resolve_duration(tool_input, config)
    except Exception:
        # Sizing the timeout must not itself fail: _run() reports the same
        # config problem a moment later. Falls back to _resolve_duration's
        # defaults, the numbers an empty [listen] section would give.
        duration = 8

    capture_timeout = duration + 5
    transcribe_timeout = max(
        _TRANSCRIBE_TIMEOUT_MIN, duration * _TRANSCRIBE_TIMEOUT_PER_SECOND
    )
    overall_timeout = capture_timeout + transcribe_timeout + _OUTER_TIMEOUT_MARGIN

    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_run, tool_input), timeout=overall_timeout
        )
    except TimeoutError:
        return json.dumps(
            {
                "status": "error",
                "reason": (
                    f"listen timed out after {overall_timeout}s — most "
                    "likely a hung transcription, since capture on its own "
                    f"is already bounded to {capture_timeout}s (the "
                    "underlying call could not be cancelled and may still "
                    "be running in the background — same documented "
                    "limitation as core/midi1.py's in-process calls)"
                ),
            }
        )


def _load_config() -> dict:
    try:
        with open(_CONFIG_PATH, "rb") as f:
            return tomllib.load(f).get("listen", {})
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"config.toml is not valid TOML: {e}") from e


def _run(tool_input: dict) -> str:
    try:
        config = _load_config()
    except ValueError as e:
        return json.dumps({"error": str(e)})

    # 1. `enabled` first, before touching anything else — a disabled tool
    #    should never open the microphone at all.
    if not config.get("enabled", True):
        return json.dumps(
            {
                "status": "disabled",
                "reason": "listen is disabled — set [listen].enabled = "
                "true in config.toml to turn it back on",
            }
        )

    # 2. Device configured.
    device = tool_input.get("device") or config.get("device")
    if not device:
        return json.dumps(
            {
                "status": "not_configured",
                "reason": (
                    "no microphone device configured — set "
                    "[listen].device in config.toml (see `pactl list "
                    "sources short` to find its name)"
                ),
            }
        )

    # 3. Resolve + clamp duration.
    duration = _resolve_duration(tool_input, config)

    model_size = config.get("model_size", "base")

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        wav_path = tmp.name

    try:
        # 4. Capture. `timeout` exits 124 when it cuts the recording at
        # `duration` seconds: the expected success case. The outer Python
        # `timeout=` is a few seconds longer than the command's own bound, so
        # parecord can flush and close the file after SIGTERM.
        capture = subprocess.run(
            [
                "timeout", str(duration),
                "parecord",
                f"--device={device}",
                "--file-format=wav",
                wav_path,
            ],
            capture_output=True,
            text=True,
            timeout=duration + 5,
            check=False,
        )
        if capture.returncode not in (0, 124):
            return json.dumps(
                {
                    "status": "error",
                    "reason": f"capture failed: {capture.stderr.strip()}",
                }
            )

        # 5. Transcribe in-process (no CLI entry point for faster-whisper,
        # unlike Piper — see module docstring).
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            return json.dumps(
                {
                    "error": "faster-whisper is not installed — "
                    "`pip install faster-whisper` to enable the listen "
                    "tool"
                }
            )

        model = WhisperModel(model_size, device="cpu", compute_type="int8")
        segments, info = model.transcribe(wav_path)
        transcript = " ".join(seg.text.strip() for seg in segments)

    except subprocess.TimeoutExpired as e:
        return json.dumps(
            {"status": "error", "reason": f"capture timed out: {e}"}
        )
    except Exception as e:
        return json.dumps(
            {"status": "error", "reason": f"transcription failed: {e}"}
        )
    finally:
        # Not `trash` — this is a scratch recording, not user data the
        # trash convention is meant for, same reasoning as speak.py's own
        # temp WAV cleanup.
        try:
            os.remove(wav_path)
        except OSError:
            pass

    return json.dumps(
        {
            "status": "ok",
            "transcript": transcript,
            "language": info.language,
            "language_probability": round(info.language_probability, 3),
            "duration_seconds": duration,
        }
    )
