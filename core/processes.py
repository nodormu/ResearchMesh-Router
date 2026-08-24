"""Interactive processes — pexpect, for the class of commands bash cannot run.

The bash tool pipes stdin from nowhere, so anything that asks a question
mid-run (ssh host-key confirmation, a sudo password, fdisk, an installer, a
REPL) blocks until the timeout kills it. This tool spawns the program on a
pseudo-terminal and answers its prompts from a script the model supplies up
front, which keeps it one tool instead of a stateful spawn/send/expect trio.

Requires:  pip install pexpect
"""

import asyncio
import json

from core.output import clip

TOOLS = [
    {
        "name": "interactive_run",
        "description": (
            "Run a command that PROMPTS for input, answering its prompts from a "
            "script. Use this instead of bash whenever a program asks a question "
            "mid-run — password challenges, ssh host-key confirmation, "
            "'Are you sure? [y/N]', partitioning tools, installers, or a REPL — "
            "since the bash tool has no stdin and will simply hang. Each step "
            "waits for a regex to appear, then sends a line. Returns JSON with the "
            "terminal transcript and the exit status."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": (
                        "Shell command to spawn on a pty, e.g. "
                        "'ssh user@host uptime'."
                    ),
                },
                "steps": {
                    "type": "array",
                    "description": (
                        "Prompt/response pairs, applied in order. Omit for a "
                        "command that only needs a pty and no answers."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "expect": {
                                "type": "string",
                                "description": (
                                    "Regex to wait for, e.g. 'password:' or "
                                    "'\\[y/N\\]'."
                                ),
                            },
                            "send": {
                                "type": "string",
                                "description": "Line to send once it matches.",
                            },
                            "secret": {
                                "type": "boolean",
                                "description": (
                                    "Redact this response from the returned "
                                    "transcript. Use for passwords."
                                ),
                            },
                        },
                        "required": ["expect", "send"],
                    },
                },
                "timeout": {
                    "type": "integer",
                    "description": (
                        "Seconds to wait for each prompt and for the program to "
                        "finish (default 30)."
                    ),
                },
            },
            "required": ["command"],
        },
    }
]

_TOOL_NAMES = {t["name"] for t in TOOLS}
_MAX_TRANSCRIPT = 12000
_DEFAULT_TIMEOUT = 30


def handles(name: str) -> bool:
    return name in _TOOL_NAMES


async def execute(name: str, tool_input: dict) -> str:
    if name != "interactive_run":
        return json.dumps({"error": f"unknown process tool {name!r}"})
    return await asyncio.to_thread(_run, tool_input)


def _run(tool_input: dict) -> str:
    try:
        import pexpect
    except ImportError:
        return json.dumps(
            {
                "error": "pexpect is not installed — `pip install pexpect` to "
                "enable the interactive_run tool"
            }
        )

    command = tool_input.get("command", "")
    if not command.strip():
        return json.dumps({"error": "no command provided"})

    steps = tool_input.get("steps") or []
    timeout = int(tool_input.get("timeout") or _DEFAULT_TIMEOUT)

    transcript: list[str] = []
    matched = 0
    timed_out_at = None

    try:
        child = pexpect.spawn(
            "/bin/bash",
            ["-c", command],
            encoding="utf-8",
            codec_errors="replace",
            timeout=timeout,
            echo=False,
        )
    except Exception as e:
        return json.dumps({"error": f"could not spawn command: {e}"})

    try:
        for step in steps:
            pattern = str(step.get("expect", ""))
            try:
                child.expect(pattern)
            except pexpect.TIMEOUT:
                timed_out_at = pattern
                break
            except pexpect.EOF:
                timed_out_at = None
                transcript.append(child.before or "")
                break

            transcript.append((child.before or "") + (child.after or ""))
            reply = str(step.get("send", ""))
            child.sendline(reply)
            transcript.append("***\n" if step.get("secret") else reply + "\n")
            matched += 1

        # Drain whatever the program prints after the last answer.
        try:
            child.expect(pexpect.EOF)
            transcript.append(child.before or "")
        except pexpect.TIMEOUT:
            transcript.append(child.before or "")
            if timed_out_at is None:
                timed_out_at = "(waiting for the program to exit)"
    finally:
        try:
            child.close(force=True)
        except (OSError, pexpect.ExceptionPexpect) as e:
            print(f"[processes] child.close failed (ignored): {e}")

    return json.dumps(
        {
            "transcript": clip("".join(transcript), _MAX_TRANSCRIPT),
            "steps_matched": matched,
            "steps_total": len(steps),
            "exit_status": child.exitstatus,
            "signal_status": child.signalstatus,
            "timed_out_waiting_for": timed_out_at,
        }
    )
