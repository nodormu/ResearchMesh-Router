"""Record audio from your own configured microphone for a bounded window and
transcribe it locally via faster-whisper (CPU, no cloud STT).

Same motivation and shape as `speak.py`/`vision.py`/`text_embeddings.py`:
self-hosted, config-driven, declared to Claude either way so a fresh clone
doesn't need a code change to gain the capability once configured. It does
nothing until `config.toml` has `[listen]` set up — see that block for
what's required.

Two independent reasons this can decline to record, both returned as a
`status` field rather than raised, matching `speak.py`'s pattern:
  - `"disabled"`       — `[listen].enabled` is explicitly false. Checked
                          BEFORE `device`, so a disabled tool never opens
                          the microphone at all. This is a genuinely
                          different switch from the one below: it can be
                          flipped even when a device is fully configured
                          and working — e.g. to guarantee the mic stays
                          closed for a while without losing/unsetting
                          `device`.
  - `"not_configured"` — `[listen].device` is unset. Same "not wired up
                          yet, here's what to do about it" shape
                          `vision_query`/`speak` use for a missing
                          server URL / voice model.

Settings are re-read from config.toml on every call (not cached at import),
so editing `enabled`/`device`/`model_size`/durations takes effect on the
very next call, no restart needed — same as every other config-driven tool
here.

Real asymmetry from `speak.py`, worth knowing before touching this file:
`speak.py` shells out to TWO subprocesses (`python3 -m piper`, then
`paplay`) because Piper is designed as a CLI tool. `faster-whisper` has no
equivalent CLI entry point — it's a Python library, used via
`from faster_whisper import WhisperModel` directly IN-PROCESS. So this
module has ONE subprocess call (`timeout <N> parecord ...`, for capture)
and ONE in-process library call (`WhisperModel(...).transcribe(...)`, for
transcription) — not two subprocesses.

⚠️ `timeout <N> parecord ...` exits with code 124 when it cuts the
recording off after N seconds — THAT IS THE NORMAL, EXPECTED SUCCESS CASE
here, not a failure (confirmed empirically: the WAV file is still written
correctly). Only OTHER non-zero exit codes (device busy, bad device name,
etc.) indicate a real capture failure. Do not naively treat any non-zero
exit as an error — this was checked live before writing this file, not
assumed.

Requires:  pip install faster-whisper   (already installed and proven this
                                          session — CPU/int8, no GPU needed)
           A working PipeWire microphone source — see
           camera_mic_hardware_testing_plan.md (in /memories) for the
           `pactl list sources short` device-discovery command and the
           gain-tuning note (mic input volume matters a lot for accuracy).
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
                        "Optional PipeWire source name to override "
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


async def execute(name: str, tool_input: dict) -> str:
    if name != "listen":
        return json.dumps({"error": f"unknown listen tool {name!r}"})
    return await asyncio.to_thread(_run, tool_input)


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
    duration = tool_input.get("duration_seconds") or config.get(
        "default_duration_seconds", 8
    )
    max_duration = config.get("max_duration_seconds", 30)
    duration = max(1, min(int(duration), int(max_duration)))

    model_size = config.get("model_size", "base")

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        wav_path = tmp.name

    try:
        # 4. Capture. `timeout` exits 124 when it cuts the recording off
        # after `duration` seconds — that's the expected success case, not
        # an error (confirmed empirically, see module docstring). The
        # outer Python `timeout=` is a few seconds LONGER than the
        # `timeout` command's own bound, so parecord gets a chance to
        # flush/close the file cleanly after SIGTERM rather than racing an
        # equally-tight outer deadline.
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
