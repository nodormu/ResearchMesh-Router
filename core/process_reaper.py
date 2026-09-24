"""Last-line safety net for process exit: verify the real OS child-process
tree is actually empty, independent of what any individual tool's own
`shutdown()`/`close()` claims to have cleaned up.

Every local tool that spawns a subprocess (bash_session's persistent bash,
the IPython kernel, Playwright's browser, an MCP server launched via stdio)
is responsible for closing its own resource, and does. This module doesn't
replace that — it runs LAST, after all of it, and checks the one thing none
of those individually can: whether anything is *still* alive regardless.
That catches what none of them can see on their own — a background job a
`bash_session` command started (`sleep 300 &` survives across tool calls by
design, but has no reason to survive the whole app exiting), or a future
tool that spawns a subprocess without wiring up its own cleanup at all.

`/proc/<pid>/task/<pid>/children` is Linux-only (this app already is) and
needs no extra dependency — added in a mainline kernel over a decade ago,
but still read defensively in case it's ever missing, so a kernel without
it just means this step quietly finds nothing rather than crashing exit.

SIGKILL, not SIGTERM: by this point every tool has already had its own
chance at a clean, negotiated shutdown. Anything still here didn't take
it, so — same reasoning as bash_session's own `_kill_foreground()` — this
doesn't negotiate either.
"""

import os
import signal
import time


def _children_of(pid: int) -> list[int]:
    """`/proc/<pid>/task/<TID>/children` is PER-THREAD, not per-process —
    it only lists children forked BY that specific thread. Checking only
    `task/<pid>/children` (the main thread) silently misses anything
    forked from a worker thread — confirmed live this is not a hypothetical:
    bash_session's real spawn always runs inside `asyncio.to_thread()`
    (true of every tool that blocks in this app — kernel, computer, listen,
    speak all use the same pattern), so its child showed up in `/proc/
    <child>/status`'s own PPid line as unambiguously ours, while the
    naive single-thread check found nothing at all. Every thread under
    `/proc/<pid>/task/` has to be checked to actually catch this."""
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
    """Clear any DIRECT child of the current process already killed but
    not yet waited on — SIGKILL alone leaves a zombie in the process table
    until its parent collects the exit status. `-1` means "any child of
    this process," not a specific pid — passing our OWN pid here instead
    would be a no-op (confirmed live while building this: it silently did
    nothing, since a process is never its own child).

    SIGKILL does not transition its target to zombie state instantly —
    confirmed live that a single WNOHANG attempt right after the kill can
    still return `(0, 0)` ("a child exists, nothing to reap yet") even
    though that same child reliably shows up moments later. So this
    retries for a bounded window instead of giving up on the first miss —
    bounded because this must never be able to hang the whole app's exit,
    only ever add up to this much delay to it.

    Deeper (non-direct) descendants don't need this: once their own
    immediate parent is also gone, the kernel reparents and auto-reaps
    them — confirmed live, they were already gone from the descendant list
    before this function ever ran."""
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
