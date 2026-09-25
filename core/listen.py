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


# `faster-whisper`'s `model.transcribe()` returns a LAZY generator for
# segments (confirmed live via its own source: `generate_segments` itself
# contains `yield`) — the real per-segment decoding work happens when the
# caller iterates it, not when `transcribe()` is called. `model.transcribe()`
# alone does real work too (feature extraction, language detection), but a
# timeout that only wrapped that call and not the segment consumption right
# after it would be protecting the wrong, shorter part of the work and leave
# the actual long-running part completely unguarded — confirmed by timing
# both parts separately on a realistic-length clip before writing this.
#
# faster-whisper has no CLI entry point (see module docstring), so unlike
# the capture step above, there is no subprocess here to SIGKILL if this
# hangs — same fundamental limitation core/midi1.py already documents for
# its own in-process C-extension calls: `asyncio.wait_for` bounds the
# CALLER'S wait, but cannot force the underlying thread to stop.
#
# Budget: measured ~2.6s total (model load + transcribe + join) for a full
# 30s clip on this machine — call it ~12x real-time. `max(60, duration*4)`
# is a generous multiple of that with real margin for a slower CPU or a
# bigger configured model_size, while still bounding a genuinely
# pathological hang to a bounded wait rather than an unbounded one.
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
        # Timeout SIZING must not itself be able to fail — _run() will hit
        # and report the SAME config problem properly a moment later; this
        # just needs a sane fallback so that failure doesn't also break the
        # ability to time out at all. Matches _resolve_duration's own
        # hardcoded defaults, so this is the same number config would have
        # produced anyway on an empty/missing [listen] section.
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
