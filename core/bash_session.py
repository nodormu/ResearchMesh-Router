"""Persistent bash: one shell process that survives across tool calls.

The `bash` tool spawns a fresh subprocess per call, so `cd`, exports, venvs,
functions and background jobs die with it. This module keeps one shell alive,
driven over a pty with pexpect (as core/processes.py does) and held as a
singleton like the kernel in core/kernel.py.

Framing: each command runs inside a brace group together with a `printf` of a
random per-spawn sentinel plus `$?`; `expect()` on the sentinel marks where
output ends and what it returned. The group also resets PS1 and the prompt
hook, so a command that changes the prompt (venv, conda, direnv) cannot leak
prompt text into the output.

Shells: bash (default), zsh and dash, chosen by `[bash].shell` through
`SHELL_EXECUTABLE` and `apply_shell_prelude` in core/claude_learned_schemas.py.
fish, tcsh and ksh are unsupported: they hang or need different grouping
syntax. zsh needs `_ZSH_SESSION_PRELUDE` (disable its line editor, which
redraws lines with backspaces) and a `precmd()` reset in place of
`PROMPT_COMMAND`.

Not a replacement for `bash` (one-off commands) or `interactive_run`: a
foreground command that blocks on its own stdin (password prompt, pager, REPL,
`vim`) hangs until the per-call timeout. After a timeout `_handle_timeout()`
sends Ctrl-C; if bash does not get the terminal back it respawns a fresh shell,
and the result carries `state_reset: true` (cd, env and background jobs were
lost).
"""

import asyncio
import json
import os
import re
import signal
import uuid
from pathlib import Path

from core.claude_learned_schemas import SHELL_EXECUTABLE, apply_shell_prelude
from core.output import clip

# Same zsh check as apply_shell_prelude(); needed here because the PS1-leak
# defense differs on zsh.
_IS_ZSH = Path(SHELL_EXECUTABLE).name == "zsh"

# dash is Ubuntu/Debian's /bin/sh, so anything that shells out via /bin/sh runs
# under it. It reaches this module the same way zsh does ([bash].shell =
# "dash").
_IS_DASH = Path(SHELL_EXECUTABLE).name == "dash"

# `PROMPT_COMMAND` is bash-only. zsh calls a `precmd` function before each new
# top-level prompt, so it needs the same hook-based reset. dash has no prompt
# hook at all: it reads $PS1 fresh when it prints a prompt, so a plain `PS1=''`
# as the last statement in the brace group is enough.
if _IS_ZSH:
    _PS1_RESET = "precmd() { PS1=''; }"
elif _IS_DASH:
    _PS1_RESET = "PS1=''"
else:
    _PS1_RESET = "PROMPT_COMMAND='PS1=\"\"'"

# zsh-only spawn-time priming, sent first; empty on bash.
# `unsetopt zle`: zsh's line editor redraws lines with backspace sequences that
# the ANSI stripping cannot handle ("printf" arrived as "print ff"). Must be
# the first thing sent.
# `promptcr promptsp`: cosmetic; removes the PROMPT_EOL_MARK `%` and its
# padding.
_ZSH_SESSION_PRELUDE = "unsetopt zle promptcr promptsp\n" if _IS_ZSH else ""

# A bare interactive shell loads ~/.bashrc, which sets a coloured PS1 and an
# OSC title escape, and bash turns on bracketed-paste mode. None of it is
# driven by our commands, so it is suppressed once at spawn (bracketed paste,
# PROMPT_COMMAND, PS1) and also stripped from captured output.
_ANSI = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-9;?]*[a-zA-Z]")

TOOLS = [
    {
        "name": "bash_session",
        "description": (
            "Run a command in a PERSISTENT bash shell. Unlike the `bash` tool, "
            "which spawns a fresh subprocess every call, this keeps the SAME "
            "shell alive across calls — `cd`, exported environment variables, "
            "sourced virtualenvs/`nvm`/etc., shell functions, and background "
            "jobs (`command &`) all survive from one call to the next. Use "
            "plain `bash` for quick one-off commands; use this when you need "
            "that persistence (e.g. `cd` into a project once and run several "
            "commands relative to it, or activate a venv and keep using it). "
            "stdout and stderr are merged into one `output` string, same as a "
            "real terminal, since this runs on a real pty. Like `bash`, a "
            "command that blocks waiting on its own stdin (a password prompt, "
            "`read`, an installer) will hang until `timeout` — use "
            "`interactive_run` for those instead. Pass `restart: true` (alone, "
            "as its own call) to kill and respawn the shell, discarding all "
            "state, if the session gets wedged or you want a clean environment."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": (
                        "Shell command to run in the persistent session. May "
                        "be multi-line (e.g. a for-loop or heredoc)."
                    ),
                },
                "timeout": {
                    "type": "integer",
                    "description": (
                        "Seconds to wait for this command to finish (default "
                        "120). On timeout, the session attempts to recover "
                        "with Ctrl-C so the NEXT call isn't necessarily also "
                        "stuck — check the `recovered` field."
                    ),
                },
                "restart": {
                    "type": "boolean",
                    "description": (
                        "Kill and respawn the shell, discarding cwd/env/"
                        "background jobs. Send this alone, without `command`, "
                        "in its own call."
                    ),
                },
            },
        },
    }
]

_TOOL_NAMES = {t["name"] for t in TOOLS}
_MAX_OUTPUT = 12000
_DEFAULT_TIMEOUT = 120
_RECOVERY_TIMEOUT = 10

try:
    import pexpect
except ImportError:  # pragma: no cover - pexpect is a hard dependency; this
    # Degrades to an install hint when pexpect is missing, as core/processes.py
    # does.
    pexpect = None

_shell = None
_sentinel = None


def handles(name: str) -> bool:
    return name in _TOOL_NAMES


async def execute(name: str, tool_input: dict) -> str:
    if name != "bash_session":
        return json.dumps({"error": f"unknown bash_session tool {name!r}"})
    return await asyncio.to_thread(_run, tool_input)


def _sentinel_pattern() -> str:
    # Only called after `_spawn()` has set `_sentinel`; the check narrows the
    # type for mypy.
    assert _sentinel is not None
    return re.escape(_sentinel) + r":(-?\d+)"


def _spawn() -> str | None:
    """(Re)start the persistent shell. Returns an error string, or None."""
    global _shell, _sentinel

    if pexpect is None:
        return "pexpect is not installed — `pip install pexpect` to enable the bash_session tool"

    _shutdown_sync()
    _sentinel = uuid.uuid4().hex

    try:
        shell = pexpect.spawn(
            SHELL_EXECUTABLE,
            encoding="utf-8",
            codec_errors="replace",
            echo=False,
            timeout=_DEFAULT_TIMEOUT,
        )
        # Quiet what ~/.bashrc turns on in an interactive shell:
        # bracketed-paste mode, OSC window-title sequences in PS1 and
        # PROMPT_COMMAND, PS1, and PS2 (blanked so "> " does not leak into
        # multi-line commands). `bind` is bash-only and fails silently
        # elsewhere. The zsh prelude is applied once here, since a `setopt`
        # stays in effect for the shell's life. The same line is the initial
        # sentinel round trip, so the startup banner never reaches command #1's
        # output. PROMPT_COMMAND is armed with a PS1-blanking hook, not unset;
        # see the per-call reset in `_run()`.
        if _ZSH_SESSION_PRELUDE:
            # Its own round trip, not folded into the priming line below: zsh's
            # line editor is still reading this line, so turning it off
            # mid-line leaves the rest of a multi-line sendline() unread and
            # hangs the next expect(). Waiting for this line's echo guarantees
            # the plain reader is active.
            shell.sendline(_ZSH_SESSION_PRELUDE.rstrip("\n"))
            shell.expect_exact(_ZSH_SESSION_PRELUDE.rstrip("\n"), timeout=_DEFAULT_TIMEOUT)
        primed = apply_shell_prelude("true")
        shell.sendline(
            "bind 'set enable-bracketed-paste off' 2>/dev/null; "
            f"{_PS1_RESET}; PS2=''\n"
            f"{primed}\nprintf '\\n{_sentinel}:%d\\n' $?"
        )
        shell.expect(_sentinel_pattern(), timeout=_DEFAULT_TIMEOUT)
    except Exception as e:
        try:
            shell.close(force=True)
        except Exception as close_error:
            print(
                f"[bash_session] cleanup after failed spawn also failed "
                f"(ignored): {close_error}"
            )
        return f"could not spawn persistent shell: {e}"

    _shell = shell
    return None


def _run(tool_input: dict) -> str:
    if tool_input.get("restart"):
        error = _spawn()
        if error:
            return json.dumps({"error": error})
        return json.dumps({"restarted": True})

    if _shell is None:
        error = _spawn()
        if error:
            return json.dumps({"error": error})

    # Set by `_spawn()`, which returns an error string on failure, so this is
    # unreachable in practice; it narrows the type for mypy.
    if _shell is None:
        return json.dumps({"error": "shell is not running"})

    command = tool_input.get("command", "")
    if not command.strip():
        return json.dumps({"error": "no command provided"})

    timeout = int(tool_input.get("timeout") or _DEFAULT_TIMEOUT)

    try:
        # The command and the reset/sentinel share one brace group, not two
        # top-level lines. Bash runs PROMPT_COMMAND (and prints PS1) only
        # before reading a new top-level command; inside an open compound
        # construct every line is read via PS2, which is blanked at spawn.
        # Keeping the reset in the group means it runs before any real prompt,
        # even for commands that reassign PROMPT_COMMAND (conda, direnv). A
        # `;`-join breaks on a trailing `#` comment or a heredoc. The group
        # does not fork, so cd, export and functions persist; a bare `exit`
        # still kills the shell (the `pexpect.EOF` branch).
        # `$?` is captured first, in a variable named from the sentinel so it
        # cannot collide with the command's own.
        _shell.sendline(
            "{\n"
            f"{command}\n"
            f"__rc_{_sentinel}=$?; {_PS1_RESET}; PS2=''; "
            f"printf '\\n{_sentinel}:%d\\n' $__rc_{_sentinel}\n"
            "}"
        )
        _shell.expect(_sentinel_pattern(), timeout=timeout)
    except pexpect.TIMEOUT:
        return json.dumps(_handle_timeout())
    except pexpect.EOF:
        partial = _clean(_shell.before or "")
        _shutdown_sync()
        return json.dumps(
            {
                "output": clip(partial, _MAX_OUTPUT),
                "error": "the persistent shell exited unexpectedly; it will "
                "respawn fresh on the next call",
            }
        )

    output = _clean(_shell.before or "")
    return_code = int(_shell.match.group(1))
    return json.dumps(
        {
            "output": clip(output, _MAX_OUTPUT) or "(no output)",
            "return_code": return_code,
        }
    )


def _clean(text: str) -> str:
    """Strip ANSI/OSC escapes and normalize CRLF -> LF."""
    return _ANSI.sub("", text).replace("\r\n", "\n").replace("\r", "")


def _bash_owns_foreground(bash_pid: int, fd: int) -> bool:
    """Is bash itself reading this pty, or is something else?

    Authoritative after a timeout: a regex match on output is not proof,
    because a raw-mode program (`less`) can echo our recovery keystrokes and
    reproduce the sentinel while still owning the terminal. Asks the kernel who
    owns the pty's foreground.
    """
    try:
        return os.tcgetpgrp(fd) == os.getpgid(bash_pid)
    except OSError:
        return False


def _kill_foreground(bash_pid: int, fd: int) -> bool:
    """Best-effort SIGKILL of whatever owns the tty if it is not bash. A
    courtesy before abandoning this pty (see `_handle_timeout()`), not what
    makes the session safe to reuse. Returns whether a kill was sent.
    """
    try:
        fg_pgid = os.tcgetpgrp(fd)
        if fg_pgid == os.getpgid(bash_pid):
            return False
        os.killpg(fg_pgid, signal.SIGKILL)
        return True
    except OSError:
        return False


def _handle_timeout() -> dict:
    """A command exceeded its timeout.

    Layer 1: Ctrl-C. A plain blocking command (`sleep`, `curl`) dies to it and
    the shell carries on with its state. Recovery counts only if the sentinel
    matches AND bash owns the terminal again (`_bash_owns_foreground`): a
    raw-mode program (`less`, `vim`, `top`, `psql`, REPLs) can echo our
    keystrokes back and reproduce the sentinel while still running.

    Layer 2: otherwise kill what is left and respawn a fresh shell via
    `_spawn()`, the path `restart: true` uses. The old pty is not reused: after
    a forced kill a stale byte can surface on the next command (exit code 130
    on a bare `echo`), a line-discipline race no heuristic closes. The result
    carries `state_reset: true` because cd, env and background jobs are lost.
    """
    # Called from `_run()`'s `except pexpect.TIMEOUT:` handler. Reads the
    # global fresh, so the None check is repeated for mypy.
    assert _shell is not None
    partial = _clean(_shell.before or "")
    bash_pid, fd = _shell.pid, _shell.child_fd

    # Layer 1: the polite attempt, gated on ground truth, not the sentinel
    # match alone.
    try:
        _shell.sendcontrol("c")
        _shell.sendline(
            "{\n"
            f"{_PS1_RESET}; PS2=''; "
            # 130 comes in through a variable and %d, not as a literal digit
            # sequence in the printf source: a shell that echoes its input
            # (zsh's plain reader does) would otherwise let expect() match the
            # sentinel inside the echoed source, before the command has run.
            # Same shape as the per-call path in `_run()`.
            f"__rc_{_sentinel}=130; printf '\\n{_sentinel}:%d\\n' $__rc_{_sentinel}\n"
            "}"
        )
        _shell.expect(_sentinel_pattern(), timeout=_RECOVERY_TIMEOUT)
        recovered = _bash_owns_foreground(bash_pid, fd)
    except Exception:
        recovered = False

    force_killed = False
    state_reset = False
    if not recovered:
        # Layer 2: do not keep negotiating with this pty. Kill what is left and
        # start over through the path `restart` uses.
        force_killed = _kill_foreground(bash_pid, fd)
        error = _spawn()
        recovered = error is None
        state_reset = recovered

    return {
        "output": clip(partial, _MAX_OUTPUT),
        "timed_out": True,
        "recovered": recovered,
        "force_killed": force_killed,
        "state_reset": state_reset,
        "return_code": None,
    }


def _shutdown_sync():
    global _shell, _sentinel
    if _shell is not None:
        try:
            _shell.close(force=True)
        except Exception as e:
            print(f"[bash_session] shell close on shutdown failed (ignored): {e}")
    _shell = None
    _sentinel = None


async def shutdown():
    """Kill the persistent shell. Safe to call even if it was never started."""
    if _shell is None:
        return
    await asyncio.to_thread(_shutdown_sync)
