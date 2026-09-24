"""Behavioural regression tests for core/listen.py's timeout wrapping.

    python test_listen.py

Doesn't touch real microphone hardware — `_run()` is monkeypatched to
simulate a hang or a quick success, since the actual gap this closes is in
`execute()`'s own timeout wrapping, not in the capture/transcription logic
itself (which needs a real configured device to exercise for real).

Found by reading faster-whisper's own source, not assumed: `model.
transcribe()` returns a LAZY generator for segments (`generate_segments`
itself contains `yield`) — the real per-segment decoding work happens when
the caller ITERATES it, not when `transcribe()` is called. The old code had
no timeout around either the call or the iteration; a genuinely hung
transcription would block the tool call forever, with no error, no
recovery — unlike the capture step right before it, which was always
bounded via `subprocess.run(timeout=...)`.
"""

import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name}{' — ' + detail if detail else ''}")
        FAILURES.append(name)


def check_duration_resolution(listen) -> None:
    print("_resolve_duration: shared clamping logic, used by both execute() and _run()")
    check(
        "default duration used when unset",
        listen._resolve_duration({}, {"default_duration_seconds": 8, "max_duration_seconds": 30})
        == 8,
    )
    check(
        "explicit duration_seconds honored",
        listen._resolve_duration({"duration_seconds": 15}, {"max_duration_seconds": 30}) == 15,
    )
    check(
        "clamped to max_duration_seconds even if the caller asks for more",
        listen._resolve_duration({"duration_seconds": 999}, {"max_duration_seconds": 30}) == 30,
    )
    check(
        "clamped to at least 1",
        # NOT duration_seconds=0 -- `0 or default` evaluates falsy in
        # Python, so an explicit 0 is (pre-existing behavior, confirmed
        # against git HEAD, not something this fix changed) silently
        # treated as "not provided" and replaced by the default instead of
        # ever reaching the floor clamp. A negative value is truthy, so it
        # actually exercises max(1, ...) the way this check means to.
        listen._resolve_duration({"duration_seconds": -5}, {}) == 1,
    )


def check_timeout_math(listen) -> None:
    print("outer timeout is comfortably larger than the capture step's own inner timeout")
    duration = 8
    capture_timeout = duration + 5
    transcribe_timeout = max(
        listen._TRANSCRIBE_TIMEOUT_MIN, duration * listen._TRANSCRIBE_TIMEOUT_PER_SECOND
    )
    overall = capture_timeout + transcribe_timeout + listen._OUTER_TIMEOUT_MARGIN
    check(
        "outer timeout strictly greater than capture's own inner timeout",
        overall > capture_timeout,
        f"overall={overall}, capture_timeout={capture_timeout}",
    )
    check(
        "floor applies for a short requested duration (doesn't scale below the minimum)",
        max(listen._TRANSCRIBE_TIMEOUT_MIN, 1 * listen._TRANSCRIBE_TIMEOUT_PER_SECOND)
        == listen._TRANSCRIBE_TIMEOUT_MIN,
    )


async def check_hang_is_bounded(listen) -> None:
    print(
        "a genuinely hung _run() (simulating a stuck transcription) is bounded by "
        "execute()'s own outer timeout, not left to run forever"
    )
    orig_run = listen._run
    orig_min = listen._TRANSCRIBE_TIMEOUT_MIN
    # Lower the floor just so this test doesn't take 78s -- the FORMULA
    # being tested is identical, only the constant is smaller here.
    listen._TRANSCRIBE_TIMEOUT_MIN = 2

    def hung_run(tool_input):
        time.sleep(30)
        return json.dumps({"status": "ok", "transcript": "should never get here"})

    listen._run = hung_run
    try:
        t0 = time.monotonic()
        result = await listen.execute("listen", {"duration_seconds": 1})
        elapsed = time.monotonic() - t0
        parsed = json.loads(result)

        check(
            "returns well before the full 30s hang would have taken",
            elapsed < 20,
            f"took {elapsed:.1f}s",
        )
        check("reports status: error", parsed.get("status") == "error", str(parsed))
        check(
            "error message names the likely cause",
            "transcription" in parsed.get("reason", "").lower(),
            str(parsed),
        )
    finally:
        listen._run = orig_run
        listen._TRANSCRIBE_TIMEOUT_MIN = orig_min


async def check_quick_success_passes_through_unaffected(listen) -> None:
    print("a normal, quick, successful call passes straight through with no added latency")
    orig_run = listen._run

    def quick_run(tool_input):
        return json.dumps(
            {
                "status": "ok",
                "transcript": "hello world",
                "language": "en",
                "language_probability": 0.99,
                "duration_seconds": 8,
            }
        )

    listen._run = quick_run
    try:
        t0 = time.monotonic()
        result = await listen.execute("listen", {"duration_seconds": 8})
        elapsed = time.monotonic() - t0
        parsed = json.loads(result)

        check("returns almost instantly", elapsed < 1.0, f"took {elapsed:.3f}s")
        check("real result passed through unmodified", parsed.get("transcript") == "hello world", str(parsed))
        check("status is ok, not error", parsed.get("status") == "ok", str(parsed))
    finally:
        listen._run = orig_run


async def check_bad_config_does_not_break_timeout_sizing(listen) -> None:
    print("a config load failure while SIZING the timeout still falls back sanely, not fatally")
    orig_load = listen._load_config

    def broken_load():
        raise ValueError("config.toml is not valid TOML: simulated")

    listen._load_config = broken_load
    orig_run = listen._run
    listen._run = lambda tool_input: json.dumps({"error": "config.toml is not valid TOML"})
    try:
        # Must not raise while just computing the outer timeout -- the
        # real error still surfaces from _run() itself a moment later.
        result = await listen.execute("listen", {})
        parsed = json.loads(result)
        check(
            "execute() itself didn't crash sizing the timeout",
            "error" in parsed,
            str(parsed),
        )
    finally:
        listen._load_config = orig_load
        listen._run = orig_run


async def _run_all() -> int:
    sys.path.insert(0, str(ROOT))
    from core import listen

    check_duration_resolution(listen)
    print()
    check_timeout_math(listen)
    print()
    await check_hang_is_bounded(listen)
    print()
    await check_quick_success_passes_through_unaffected(listen)
    print()
    await check_bad_config_does_not_break_timeout_sizing(listen)
    print()

    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): {', '.join(FAILURES)}")
        return 1
    print("all checks passed")
    return 0


def main() -> int:
    return asyncio.run(_run_all())


if __name__ == "__main__":
    sys.exit(main())
