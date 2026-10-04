"""Last-line safety net at process exit: verify the OS child-process tree is
empty, whatever each tool's own `shutdown()` claims.

Every local tool that spawns a subprocess closes its own resource; this runs
last and checks the one thing they cannot: whether anything is still alive,
such as a background job started in `bash_session` (`sleep 300 &` survives
across calls by design) or a tool with no cleanup.

`/proc/<pid>/task/<pid>/children` is Linux-only and needs no extra dependency;
it is read defensively, so a kernel without it finds nothing instead of
crashing exit.

SIGKILL, not SIGTERM: every tool has already had its chance at a clean
shutdown, so this does not negotiate (as `_kill_foreground()` in bash_session
does not).
"""

import os
import signal
import time


def _children_of(pid: int) -> list[int]:
    """`/proc/<pid>/task/<TID>/children` is per thread: it lists only children
    forked by that thread. Every thread under `/proc/<pid>/task/` must be
    checked, because blocking tools spawn from `asyncio.to_thread()` workers
    (bash_session, kernel, computer, listen, speak), whose children a
    main-thread-only check misses.
    """
    children: list[int] = []
    try:
        task_ids = os.listdir(f"/proc/{pid}/task")
    except (FileNotFoundError, ProcessLookupError, OSError):
        return children
    for tid in task_ids:
        try:
            with open(f"/proc/{pid}/task/{tid}/children") as f:
                children.extend(int(x) for x in f.read().split())
        except (FileNotFoundError, ProcessLookupError, OSError, ValueError):
            continue
    return children


def _collect_descendants(pid: int) -> list[int]:
    """Every process still alive under `pid`, at every depth — not just
    direct children. Collected fully before anything is killed, so kill
    order can't cause a deeper descendant to be missed (see module
    docstring: a parent dying doesn't take its own children with it)."""
    descendants: list[int] = []
    for child in _children_of(pid):
        descendants.append(child)
        descendants.extend(_collect_descendants(child))
    return descendants


def _reap_zombies() -> None:
    """Reap any direct child already killed but not waited on; SIGKILL alone
    leaves a zombie until its parent collects the status. `-1` means any
    child of this process (our own pid would reap nothing, since a process
    is not its own child).

    A zombie does not appear instantly after SIGKILL: one WNOHANG right after
    the kill can return `(0, 0)` while the child is still on its way. So this
    retries for a bounded window, bounded because it must never hang exit.
    Deeper descendants need nothing: once their parent is gone the kernel
    reparents and reaps them.
    """
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        try:
            reaped_pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return  # no children left at all -- already clean
        if reaped_pid == 0:
            time.sleep(0.02)  # nothing ready yet -- give the kernel a moment
        # else: reaped one -- loop again immediately, more may already be ready


def reap_orphans() -> list[str]:
    """Force-kill every real descendant process still alive, regardless of
    what any tool's own cleanup believes it already handled. Returns a
    short description of each thing actually found and killed — empty if
    the tree was already clean, which is the expected common case."""
    pid = os.getpid()
    reaped = []
    for descendant in _collect_descendants(pid):
        try:
            with open(f"/proc/{descendant}/comm") as f:
                name = f.read().strip()
        except (FileNotFoundError, ProcessLookupError, OSError):
            continue  # already gone on its own -- nothing to report
        try:
            os.kill(descendant, signal.SIGKILL)
            reaped.append(f"{name}(pid {descendant})")
        except ProcessLookupError:
            continue  # died between the comm-read and the kill -- fine
        except OSError as e:
            reaped.append(f"{name}(pid {descendant}, kill failed: {e})")
    _reap_zombies()
    return reaped
