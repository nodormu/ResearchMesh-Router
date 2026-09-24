"""Behavioural regression tests for core/bash_session.py.

    python test_bash_session.py

Unlike smoke_test.py (wiring only — imports, tool registry, doc/code drift),
this spawns the real persistent shell and exercises it: exit-code fidelity,
the PS1/PROMPT_COMMAND leak fix (closed via the brace-group wrap in
`_run()` — verified for plain venv activation AND for commands that
reassign PROMPT_COMMAND itself, e.g. conda/direnv-style hooks),
cd/export/background-job persistence, heredoc/multi-line safety,
timeout/Ctrl-C recovery, and restart. Run after any change to
bash_session.py's sentinel-construction logic.

Spawns a real bash subprocess (via the module's own persistent shell) and
calls `bash_session.shutdown()` at the end — no real dependency beyond
`pexpect`, already required by the tool itself.
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
    # Previously a documented residual gap: a command that reassigns
    # PROMPT_COMMAND itself (conda/direnv-style activation hooks, unlike
    # plain venv which only touches PS1) still leaked on its own
    # activating call. The brace-group wrap in `_run()` closes this too —
    # bash never touches PROMPT_COMMAND while the group is still open, so
    # our own reset (inside the same group) always wins now.
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

    # The actual proof, not just the self-reported flags: a plain command
    # right after must be genuinely healthy -- correct exit code, and NOT
    # itself killed by a stray leftover signal (confirmed live this was a
    # real risk with an earlier, rejected "kill + retry same buffer"
    # design, hence the full respawn instead).
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
    """`[bash].shell` can be pointed at zsh, and bash_session already
    imports SHELL_EXECUTABLE/apply_shell_prelude from the same place the
    stateless `bash` tool does -- so zsh reaches this module today, not
    hypothetically. It used to be badly broken there: two real,
    independent bugs, both found live against a real zsh 5.9, neither
    exercised by the bash-only checks above since bash's pty session
    (echo=False) never echoes input at all, which is what let both bugs
    hide.

    Bug 1 (spawn hang): zsh's line editor (ZLE) redraws every line with
    backspace sequences this module's ANSI stripping doesn't handle --
    fixed via `unsetopt zle`. But ZLE is still the active reader for
    whatever line turns it off, so folding that into the SAME multi-line
    sendline() as the rest of _spawn()'s priming left the remainder
    unread in the pty buffer, genuinely hanging forever (reproduced
    directly). Fixed by sending it as its own round-trip first.

    Bug 2 (recovery-path corruption): zsh's non-ZLE fallback reader still
    echoes input back, even with ZLE off. _handle_timeout()'s recovery
    command used to hardcode the literal digits ":130" in its own printf
    SOURCE rather than a %d placeholder + variable -- so that fully-formed
    sentinel pattern showed up in the ECHO of the command, and expect()
    matched it there, before the command even ran. The real completion
    then bled into the NEXT call's capture (wrong return_code, garbage
    output). Fixed by using the same %d-not-literal-digits shape the main
    per-call path already used.

    Skips cleanly (not a failure) if zsh isn't installed -- this is
    coverage for an optional, opt-in shell choice, not a hard dependency.
    """
    print("zsh support ([bash].shell = zsh) -- both bugs above, fixed")

    zsh_path = shutil.which("zsh") or (
        "/usr/bin/zsh" if os.path.isfile("/usr/bin/zsh") else None
    )
    if not zsh_path:
        print("  skip  zsh not installed on this machine -- nothing to test")
        return

    # Simulate what a fresh import would compute with [bash].shell=zsh --
    # _IS_ZSH/_PS1_RESET/_ZSH_SESSION_PRELUDE are derived once at import
    # time in the real module, so a live monkeypatch has to override all
    # three together, not just SHELL_EXECUTABLE itself.
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

        # Plain Ctrl-C recovery -- this exact scenario is what exposed
        # Bug 2 above (a stale ":130" bleeding into this exact follow-up).
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
            check_raw_mode_program_recovery,
            check_restart,
            check_zsh_support,
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
