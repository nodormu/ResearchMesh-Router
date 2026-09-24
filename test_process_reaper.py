"""Behavioural regression tests for core/process_reaper.py.

    python test_process_reaper.py

Unlike smoke_test.py (wiring only — import/compile via its dynamic core/*.py
glob), this spawns real process trees and confirms the reaper actually finds
and kills them. Two real bugs were caught building this, both regression-
tested here specifically so neither can silently reappear:

1. `/proc/<pid>/task/<TID>/children` is PER-THREAD, not per-process — using
   only `task/<pid>/` (the main thread) misses anything forked from a worker
   thread. Every tool that blocks in this app (bash_session, kernel,
   computer, listen, speak) does exactly that via `asyncio.to_thread()`, so
   this is the realistic case, not an edge case — `check_multithread_fork`
   spawns a child from inside `asyncio.to_thread()` specifically, the same
   way bash_session's own `_spawn()` really does, and would have silently
   passed with the old single-thread-only implementation despite finding
   nothing at all.
2. SIGKILL doesn't transition its target to zombie state instantly — a
   single WNOHANG attempt right after killing can still report "nothing to
   reap yet" even though the same child reliably shows up moments later.
   `check_full_tree` confirms the full multi-level tree (a shell, a
   foreground job, and a background job) is genuinely gone afterward, not
   just reported as killed.
"""

import asyncio
import os
import subprocess
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


def check_noop_on_clean_process(reaper) -> None:
    print("no-op on a process with nothing to clean up")
    t0 = time.monotonic()
    reaped = reaper.reap_orphans()
    elapsed = time.monotonic() - t0
    check("returns an empty list", reaped == [], str(reaped))
    check(
        "resolves near-instantly, not the full retry-loop timeout",
        elapsed < 0.5,
        f"took {elapsed:.3f}s",
    )


def check_full_tree(reaper) -> None:
    print(
        "a real multi-level tree (shell -> foreground job + background "
        "job) is fully found, killed, AND confirmed gone -- not just "
        "reported as killed"
    )
    child = subprocess.Popen(["/bin/bash", "-c", "sleep 300 & sleep 300"])
    time.sleep(0.5)
    try:
        pid = os.getpid()
        before = reaper._collect_descendants(pid)
        check(
            "all three real processes found before reaping (bash + 2 sleeps)",
            len(before) == 3,
            str(before),
        )

        reaped = reaper.reap_orphans()
        check("reports killing exactly what was found", len(reaped) == 3, str(reaped))
        check(
            "reported names match (bash, sleep, sleep)",
            sorted(r.split("(")[0] for r in reaped) == ["bash", "sleep", "sleep"],
            str(reaped),
        )

        after = reaper._collect_descendants(pid)
        check(
            "tree is GENUINELY empty afterward, not just self-reported",
            after == [],
            str(after),
        )
    finally:
        try:
            child.wait(timeout=2)
        except Exception as e:
            print(f"  (cleanup: reaping the test's own outer child failed, ignored: {e})")


async def check_multithread_fork(reaper) -> None:
    print(
        "catches a child forked from a WORKER THREAD via asyncio.to_thread() "
        "-- the exact pattern bash_session/kernel/computer/listen/speak all "
        "use for their real blocking work, not just a main-thread subprocess.Popen()"
    )
    holder: dict = {}

    def spawn_from_worker_thread() -> None:
        # Deliberately the SAME shape as bash_session._spawn(): a real
        # fork+exec happening on a worker thread, not the main thread --
        # this is what the old task/<main_pid>/-only implementation missed.
        proc = subprocess.Popen(["/bin/bash", "-c", "sleep 300"])
        holder["proc"] = proc

    await asyncio.to_thread(spawn_from_worker_thread)
    await asyncio.sleep(0.3)

    pid = os.getpid()
    found = reaper._collect_descendants(pid)
    check(
        "the worker-thread-forked child is actually found",
        holder["proc"].pid in found,
        f"expected pid {holder['proc'].pid} in {found}",
    )

    reaped = reaper.reap_orphans()
    check("reaper kills it", len(reaped) >= 1, str(reaped))

    after = reaper._collect_descendants(pid)
    check("gone afterward", after == [], str(after))

    try:
        holder["proc"].wait(timeout=2)
    except Exception as e:
        print(f"  (cleanup: reaping the test's own outer child failed, ignored: {e})")


def check_already_dead_child_is_not_reported(reaper) -> None:
    print("a child that already exited on its own isn't falsely reported as reaped")
    proc = subprocess.Popen(["/bin/bash", "-c", "true"])
    proc.wait()  # let it exit naturally, before the reaper ever looks
    reaped = reaper.reap_orphans()
    check(
        "nothing spurious reported for an already-gone process",
        str(proc.pid) not in " ".join(reaped),
        str(reaped),
    )


async def _run_all() -> int:
    sys.path.insert(0, str(ROOT))
    from core import process_reaper as reaper

    check_noop_on_clean_process(reaper)
    print()
    check_full_tree(reaper)
    print()
    await check_multithread_fork(reaper)
    print()
    check_already_dead_child_is_not_reported(reaper)
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
