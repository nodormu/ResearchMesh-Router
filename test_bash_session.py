"""Behavioural regression tests for core/bash_session.py.

    python test_bash_session.py

Unlike smoke_test.py (wiring only), this spawns the real persistent shell and
exercises it: exit-code fidelity, the PS1/PROMPT_COMMAND leak fix (for venv
activation and for commands that reassign PROMPT_COMMAND, e.g. conda/direnv
hooks), cd/export/background-job persistence, heredoc and multi-line safety,
timeout and Ctrl-C recovery, and restart. Run after any change to the sentinel
construction in bash_session.py. Needs only `pexpect`.
"""

import asyncio
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name}{' — ' + detail if detail else ''}")
        FAILURES.append(name)


async def run(bs, command: str, timeout: int | None = None) -> dict:
    import json

    tool_input: dict[str, str | int] = {"command": command}
    if timeout is not None:
        tool_input["timeout"] = timeout
    return json.loads(await bs.execute("bash_session", tool_input))


async def check_basic_command(bs) -> None:
    print("basic command")
    r = await run(bs, "echo hello_world")
    check("output contains echoed text", "hello_world" in r.get("output", ""), str(r))
    check("return_code is 0", r.get("return_code") == 0, str(r))


async def check_exit_code_fidelity(bs) -> None:
    print("exit code fidelity (success and failure paths)")
    r = await run(bs, "true")
    check("true -> return_code 0", r.get("return_code") == 0, str(r))

    r = await run(bs, "false")
    check("false -> return_code 1", r.get("return_code") == 1, str(r))

    r = await run(bs, "(exit 42)")
    check("(exit 42) -> return_code 42", r.get("return_code") == 42, str(r))

    r = await run(bs, "ls /definitely_does_not_exist_xyz 2>/dev/null")
    check(
        "ls on a missing path -> nonzero return_code",
        isinstance(r.get("return_code"), int) and r["return_code"] != 0,
        str(r),
    )


async def check_cd_export_persistence(bs) -> None:
    print("cd/export persistence across separate calls")
    await run(bs, "cd /tmp && export BS_TEST_VAR=persist_check_42")
    r = await run(bs, "pwd; echo $BS_TEST_VAR")
    check("cwd persisted", "/tmp" in r.get("output", ""), str(r))
    check("env var persisted", "persist_check_42" in r.get("output", ""), str(r))


async def check_background_job_persistence(bs) -> None:
    print("background job survives across calls")
    logfile = "/tmp/bs_test_bg.log"
    await run(bs, f"rm -f {logfile}")
    await run(bs, f"(sleep 2 && echo bg_done > {logfile}) &")
    await asyncio.sleep(3)
    r = await run(bs, f"cat {logfile}; jobs")
    check("background job's output appeared", "bg_done" in r.get("output", ""), str(r))
    check(
        "job-done notification appeared",
        "Done" in r.get("output", ""),
        str(r),
    )
    await run(bs, f"rm -f {logfile}")


async def check_heredoc_and_multiline(bs) -> None:
    print("heredoc / multi-line for-loop (no leaked PS2 continuation prompt)")
    r = await run(bs, "cat <<'EOF'\nheredoc_line_one\nheredoc_line_two\nEOF")
    check("heredoc body present", "heredoc_line_one" in r.get("output", ""), str(r))
    check("no leaked PS2 prompt", "> " not in r.get("output", ""), str(r))

    r = await run(bs, "for i in 1 2 3; do\n  echo loop_$i\ndone")
    check("for-loop output present", "loop_1" in r.get("output", "") and "loop_3" in r.get("output", ""), str(r))
    check("no leaked PS2 prompt in loop output", "> " not in r.get("output", ""), str(r))


async def check_ps1_leak_contained(bs) -> None:
    print("PS1 leak: eliminated for a plain venv activation")
    venv = "/tmp/bs_test_ps1_venv"
    await run(bs, f"rm -rf {venv}")
    r = await run(
        bs,
        f"cd /tmp && python3 -m venv {venv} 2>&1 | tail -1 && "
        f"source {venv}/bin/activate && echo activated",
    )
    check("venv activation itself succeeded", "activated" in r.get("output", ""), str(r))
    check(
        "ACTIVATING call itself has no leaked prompt",
        "(bs_test_ps1_venv)" not in r.get("output", ""),
        str(r),
    )

    r = await run(bs, "echo call_right_after_activation")
    check(
        "NEXT call has no leaked prompt prefix/suffix",
        "(bs_test_ps1_venv)" not in r.get("output", ""),
        str(r),
    )

    r = await run(bs, "echo VIRTUAL_ENV=$VIRTUAL_ENV")
    check(
        "fix didn't break the venv itself — still active",
        venv in r.get("output", ""),
        str(r),
    )

    r = await run(bs, "false")
    check(
        "exit code still correct right after activation",
        r.get("return_code") == 1,
        str(r),
    )

    await run(bs, f"deactivate 2>/dev/null; rm -rf {venv}")


async def check_ps1_leak_prompt_command_stomp(bs) -> None:
    print("PS1 leak: closed even for a command that reassigns PROMPT_COMMAND itself")
    # A command that reassigns PROMPT_COMMAND (conda/direnv hooks; plain venv
    # only touches PS1) must not leak on its activating call: bash never runs
    # PROMPT_COMMAND while the brace group is open, so the reset inside the
    # group wins.
    r = await run(bs, "PS1='(bs_stomp_test) '; PROMPT_COMMAND='echo BS_STOMP_MARKER'")
    check(
        "activating call has no leak, even for a PROMPT_COMMAND stomp",
        "BS_STOMP_MARKER" not in r.get("output", "") and "(bs_stomp_test)" not in r.get("output", ""),
        str(r),
    )

    r = await run(bs, "echo call_right_after_stomp")
    check(
        "next call is still clean too",
        "BS_STOMP_MARKER" not in r.get("output", "") and "(bs_stomp_test)" not in r.get("output", ""),
        str(r),
    )

    r = await run(bs, "false")
    check(
        "exit code still correct right after the stomp",
        r.get("return_code") == 1,
        str(r),
    )


async def check_ps1_leak_edge_cases(bs) -> None:
    print("PS1 leak fix: previously-hazardous edge cases (comment, heredoc, multi-line stomp)")
    # A naive "just join with ';' instead of '\\n'" fix breaks on these;
    # the brace-group approach doesn't.
    r = await run(bs, "echo trailing_comment_marker # a comment, not code")
    check(
        "trailing '#' comment doesn't swallow the reset/sentinel",
        "trailing_comment_marker" in r.get("output", ""),
        str(r),
    )

    r = await run(bs, "cat <<EOF\nheredoc_after_stomp_test\nEOF")
    check(
        "heredoc still terminates correctly inside the group",
        "heredoc_after_stomp_test" in r.get("output", ""),
        str(r),
    )

    r = await run(
        bs,
        "for i in 1 2; do\n"
        "  PROMPT_COMMAND='echo LOOP_STOMP_MARKER'\n"
        "  echo iter_$i\n"
        "done",
    )
    check(
        "multi-line construct that ALSO stomps PROMPT_COMMAND mid-loop: no leak",
        "iter_1" in r.get("output", "")
        and "iter_2" in r.get("output", "")
        and "LOOP_STOMP_MARKER" not in r.get("output", ""),
        str(r),
    )


async def check_timeout_and_recovery(bs) -> None:
    print("timeout -> Ctrl-C recovery -> shell still usable, exit codes still correct")
    r = await run(bs, "sleep 30", timeout=2)
    check("reports timed_out", r.get("timed_out") is True, str(r))
    check("reports recovered", r.get("recovered") is True, str(r))

    r = await run(bs, "echo shell_responsive")
    check("shell responsive on next call", "shell_responsive" in r.get("output", ""), str(r))

    r = await run(bs, "false")
    check(
        "exit code still correct after a timeout/recovery cycle",
        r.get("return_code") == 1,
        str(r),
    )


async def check_recovery_with_sigint_ignored(bs) -> None:
    print("timeout recovery still works when the router ignores SIGINT (nohup, launchers)")
    import signal

    old_int = signal.signal(signal.SIGINT, signal.SIG_IGN)
    old_quit = signal.signal(signal.SIGQUIT, signal.SIG_IGN)
    try:
        # The shell spawned next inherits the ignored signals unless _spawn
        # resets them.
        await bs.shutdown()
        await run(bs, "export SIGIGN_MARK=kept")
        r = await run(bs, "sleep 30", timeout=2)
        check(
            "plain recovery, not escalated",
            r.get("recovered") is True
            and r.get("force_killed") is False
            and r.get("state_reset") is False,
            str(r),
        )
        r = await run(bs, "echo MARK=$SIGIGN_MARK")
        check("state survived the recovery", "MARK=kept" in r.get("output", ""), str(r))
    finally:
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGQUIT, old_quit)
        await bs.shutdown()


async def check_raw_mode_program_recovery(bs) -> None:
    print(
        "timeout recovery against a raw-mode program (less) that survives "
        "plain Ctrl-C -- escalates to a full respawn instead of a session "
        "that merely looks recovered"
    )
    # Set state BEFORE the hang, to prove the escalation path's documented
    # tradeoff: it wipes cd/env, unlike the plain-Ctrl-C path above.
    await run(bs, "cd /tmp && export BS_RAWMODE_VAR=should_not_survive_escalation")

    r = await run(bs, "printf 'line1\\nline2\\nline3\\n' | less", timeout=3)
    check("reports timed_out", r.get("timed_out") is True, str(r))
    check("reports recovered", r.get("recovered") is True, str(r))
    check("reports force_killed", r.get("force_killed") is True, str(r))
    check("reports state_reset", r.get("state_reset") is True, str(r))

    # Proof beyond the self-reported flags: a plain command right after must be
    # healthy, with the right exit code and no stray signal (a retry on the
    # same buffer was rejected for that reason; recovery respawns).
    r2 = await run(bs, "echo RAWMODE_RECOVERY_OK; false; echo rc=$?")
    check(
        "shell genuinely responsive, not just self-reported as such",
        "RAWMODE_RECOVERY_OK" in r2.get("output", ""),
        str(r2),
    )
    check(
        "exit code fidelity intact after escalated recovery",
        "rc=1" in r2.get("output", ""),
        str(r2),
    )
    check(
        "the health-check command itself wasn't killed by a leftover signal",
        r2.get("return_code") == 0,
        str(r2),
    )

    # Confirms the documented tradeoff, not an accident: cd/env from before
    # the hang did NOT survive escalation (unlike the plain-Ctrl-C case in
    # check_timeout_and_recovery above, which DOES preserve them).
    check(
        "cd did not survive the escalated recovery",
        "should_not_survive_escalation" not in r2.get("output", ""),
        str(r2),
    )


async def check_restart(bs) -> None:
    print("restart wipes cwd/env, self-heals on next use")
    await run(bs, "cd /tmp && export BS_RESTART_VAR=should_not_survive")
    result = await bs.execute("bash_session", {"restart": True})
    import json

    parsed = json.loads(result)
    check("restart reports restarted:true", parsed.get("restarted") is True, str(parsed))

    r = await run(bs, "echo BS_RESTART_VAR=$BS_RESTART_VAR")
    check("env var did NOT survive restart", "should_not_survive" not in r.get("output", ""), str(r))


async def check_zsh_support(bs) -> None:
    """zsh through `[bash].shell`: the cases bash's pty (echo=False) cannot
    show.

    1. Spawn must not hang: `unsetopt zle` is sent as its own round trip,
    because ZLE is still reading the line that turns it off and would leave the
    rest of a multi-line sendline() unread.
    2. Recovery must not corrupt: zsh's plain reader still echoes input, so
    `_handle_timeout()`'s command uses a `%d` placeholder instead of the
    literal `:130`, which expect() would otherwise match in the echo.

    Skips if zsh is not installed.
    """
    print("zsh support ([bash].shell = zsh) -- both bugs above, fixed")

    zsh_path = shutil.which("zsh") or (
        "/usr/bin/zsh" if os.path.isfile("/usr/bin/zsh") else None
    )
    if not zsh_path:
        print("  skip  zsh not installed on this machine -- nothing to test")
        return

    # Simulate a fresh import with [bash].shell=zsh: _IS_ZSH, _PS1_RESET and
    # _ZSH_SESSION_PRELUDE are derived at import time, so a monkeypatch must
    # override all three.
    orig = (bs.SHELL_EXECUTABLE, bs._IS_ZSH, bs._PS1_RESET, bs._ZSH_SESSION_PRELUDE)
    await bs.shutdown()  # drop the existing bash-backed shell first
    bs.SHELL_EXECUTABLE = zsh_path
    bs._IS_ZSH = True
    bs._PS1_RESET = "precmd() { PS1=''; }"
    bs._ZSH_SESSION_PRELUDE = "unsetopt zle promptcr promptsp\n"

    try:
        r = await run(bs, "export ZSH_MARK=zsh_state_ok; echo hello_from_zsh")
        check(
            "basic command runs cleanly under zsh (no ZLE redraw corruption)",
            "hello_from_zsh" in r.get("output", "")
            and "PPS1" not in r.get("output", "")
            and "print ff" not in r.get("output", ""),
            str(r),
        )

        # The worst case: a command changes PS1 directly AND redefines
        # zsh's own precmd hook to something that would leak -- mirrors
        # bash's conda/direnv-stomp scenario exactly.
        await run(
            bs,
            "PS1='LEAK_MARKER_$$ '; precmd() { PS1='STILL_LEAKING'; }; "
            "echo stomped",
        )
        r = await run(bs, "echo zsh_leak_check")
        check(
            "PS1/precmd stomp does not leak into the next call",
            "LEAK_MARKER" not in r.get("output", "")
            and "STILL_LEAKING" not in r.get("output", ""),
            str(r),
        )

        # Plain Ctrl-C recovery. The follow-up command would be corrupted by a
        # stale ":130" echo, so it is checked here.
        r = await run(bs, "sleep 30", timeout=2)
        check("zsh: reports timed_out", r.get("timed_out") is True, str(r))
        check(
            "zsh: plain recovery, not escalated",
            r.get("recovered") is True and r.get("force_killed") is False,
            str(r),
        )
        r2 = await run(bs, "echo ZSH_MARK=$ZSH_MARK")
        check(
            "zsh: state survived plain recovery (proves no stale-byte bleed)",
            "ZSH_MARK=zsh_state_ok" in r2.get("output", ""),
            str(r2),
        )
        check(
            "zsh: exit code fidelity intact after recovery (not the stale 130)",
            r2.get("return_code") == 0,
            str(r2),
        )

        # Escalated recovery: a SIGINT-ignoring process, raw-mode-like.
        r = await run(
            bs,
            "python3 -c \"import signal,time; "
            "signal.signal(signal.SIGINT, signal.SIG_IGN); time.sleep(9999)\"",
            timeout=4,
        )
        check("zsh: escalation reports recovered", r.get("recovered") is True, str(r))
        check("zsh: escalation reports force_killed", r.get("force_killed") is True, str(r))
        r2 = await run(bs, "echo zsh_post_escalation_ok; false; echo rc=$?")
        check(
            "zsh: genuinely responsive after escalation, not just self-reported",
            "zsh_post_escalation_ok" in r2.get("output", ""),
            str(r2),
        )
        check(
            "zsh: exit code fidelity intact after escalated recovery",
            "rc=1" in r2.get("output", "") and r2.get("return_code") == 0,
            str(r2),
        )
    finally:
        # Leave the module exactly as later/earlier steps expect it.
        await bs.shutdown()
        bs.SHELL_EXECUTABLE, bs._IS_ZSH, bs._PS1_RESET, bs._ZSH_SESSION_PRELUDE = orig


async def check_dash_support(bs) -> None:
    """dash through `[bash].shell`: it has no `PROMPT_COMMAND` or `precmd`, so
    the bash-shaped reset is inert there. A plain `PS1=''` as the last
    statement in the brace group is enough, since dash reads $PS1 fresh when
    it prints a prompt.

    Skips if dash is not installed.
    """
    print("dash support ([bash].shell = dash) -- PS1-leak fix")

    dash_path = shutil.which("dash") or (
        "/bin/dash" if os.path.isfile("/bin/dash") else None
    )
    if not dash_path:
        print("  skip  dash not installed on this machine -- nothing to test")
        return

    # Simulate a fresh import with [bash].shell=dash: _IS_DASH and _PS1_RESET
    # are derived at import time, so a monkeypatch must override both.
    # _ZSH_SESSION_PRELUDE stays "".
    orig = (bs.SHELL_EXECUTABLE, bs._IS_ZSH, bs._IS_DASH, bs._PS1_RESET)
    await bs.shutdown()  # drop the existing bash-backed shell first
    bs.SHELL_EXECUTABLE = dash_path
    bs._IS_ZSH = False
    bs._IS_DASH = True
    bs._PS1_RESET = "PS1=''"

    try:
        r = await run(bs, "export DASH_MARK=dash_state_ok; echo hello_from_dash")
        check(
            "basic command runs cleanly under dash (no leaked PS1 template)",
            "hello_from_dash" in r.get("output", "") and "\\[\\e]0;" not in r.get("output", ""),
            str(r),
        )

        # Dash's actual worst case: there's no hook to also redefine, so
        # a direct PS1 assignment by the user's own command IS the worst
        # case (unlike bash/zsh, which also need to survive a re-armed
        # hook function).
        await run(bs, "PS1='LEAK_MARKER_$$ '; echo stomped")
        r = await run(bs, "echo dash_leak_check")
        check(
            "PS1 stomp does not leak into the next call",
            "LEAK_MARKER" not in r.get("output", ""),
            str(r),
        )

        # Plain Ctrl-C recovery -- same class of scenario that exposed
        # zsh's :130 bug; confirms no analogous corruption on dash.
        r = await run(bs, "sleep 30", timeout=2)
        check("dash: reports timed_out", r.get("timed_out") is True, str(r))
        check(
            "dash: plain recovery, not escalated",
            r.get("recovered") is True and r.get("force_killed") is False,
            str(r),
        )
        r2 = await run(bs, "echo DASH_MARK=$DASH_MARK")
        check(
            "dash: state survived plain recovery (proves no stale-byte bleed)",
            "DASH_MARK=dash_state_ok" in r2.get("output", ""),
            str(r2),
        )
        check(
            "dash: exit code fidelity intact after recovery",
            r2.get("return_code") == 0,
            str(r2),
        )

        # Escalated recovery: a SIGINT-ignoring process, raw-mode-like.
        r = await run(
            bs,
            "python3 -c \"import signal,time; "
            "signal.signal(signal.SIGINT, signal.SIG_IGN); time.sleep(9999)\"",
            timeout=4,
        )
        check("dash: escalation reports recovered", r.get("recovered") is True, str(r))
        check("dash: escalation reports force_killed", r.get("force_killed") is True, str(r))
        r2 = await run(bs, "echo dash_post_escalation_ok; false; echo rc=$?")
        check(
            "dash: genuinely responsive after escalation, not just self-reported",
            "dash_post_escalation_ok" in r2.get("output", ""),
            str(r2),
        )
        check(
            "dash: exit code fidelity intact after escalated recovery",
            "rc=1" in r2.get("output", "") and r2.get("return_code") == 0,
            str(r2),
        )
    finally:
        # Leave the module exactly as later/earlier steps expect it.
        await bs.shutdown()
        bs.SHELL_EXECUTABLE, bs._IS_ZSH, bs._IS_DASH, bs._PS1_RESET = orig


async def _run_all() -> int:
    sys.path.insert(0, str(ROOT))
    from core import bash_session as bs

    try:
        for step in (
            check_basic_command,
            check_exit_code_fidelity,
            check_cd_export_persistence,
            check_background_job_persistence,
            check_heredoc_and_multiline,
            check_ps1_leak_contained,
            check_ps1_leak_prompt_command_stomp,
            check_ps1_leak_edge_cases,
            check_timeout_and_recovery,
            check_recovery_with_sigint_ignored,
            check_raw_mode_program_recovery,
            check_restart,
            check_zsh_support,
            check_dash_support,
        ):
            await step(bs)
            print()
    finally:
        await bs.shutdown()

    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): {', '.join(FAILURES)}")
        return 1
    print("all checks passed")
    return 0


def main() -> int:
    return asyncio.run(_run_all())


if __name__ == "__main__":
    sys.exit(main())
