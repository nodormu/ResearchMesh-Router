"""Behavioural regression tests for core/processes.py.

    python test_processes.py

Covers `interactive_run`'s `send_env` step field — added so a
password/token prompt can be answered without the real value ever having to
be written into the tool call itself (see the module's own docstring for the
full rationale). Spawns real `bash -c 'read -s -p ... ; echo ...'` prompts
via pexpect (through the real `_run()`, not a re-implementation) to confirm
the actual value reaches the child process correctly AND never appears in
the plain, unredacted transcript — not just that the code parses.
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name}{' — ' + detail if detail else ''}")
        FAILURES.append(name)


def call(mod, tool_input: dict) -> dict:
    result = asyncio.run(mod.execute("interactive_run", tool_input))
    return json.loads(result)


PROMPT_CMD = 'read -s -p "Enter: " val; echo "GOT:[$val]"'


def check_existing_literal_send_unaffected(mod) -> None:
    print("existing behavior: literal `send` + `secret` unaffected")
    r = call(mod, {
        "command": PROMPT_CMD,
        "steps": [{"expect": "Enter: ", "send": "plaintext123", "secret": False}],
    })
    check("no error", "error" not in r, str(r))
    check("child received the literal value", "GOT:[plaintext123]" in r.get("transcript", ""), str(r))
    check("non-secret send appears in transcript", "plaintext123" in r.get("transcript", "").split("GOT:")[0], str(r))

    r = call(mod, {
        "command": PROMPT_CMD,
        "steps": [{"expect": "Enter: ", "send": "shouldberedacted", "secret": True}],
    })
    check("secret:true still redacts the echoed send", "shouldberedacted" not in r.get("transcript", "").split("GOT:")[0], str(r))
    check("child still received the real value despite redaction", "GOT:[shouldberedacted]" in r.get("transcript", ""), str(r))


def check_send_env_happy_path(mod) -> None:
    print("send_env: real value reaches the child, never appears unredacted in transcript")
    os.environ["_TEST_INTERACTIVE_RUN_SECRET"] = "s3cr3t-from-env-9f8a"
    try:
        r = call(mod, {
            "command": PROMPT_CMD,
            "steps": [{"expect": "Enter: ", "send_env": "_TEST_INTERACTIVE_RUN_SECRET"}],
        })
        check("no error", "error" not in r, str(r))
        check("child received the real env value", "GOT:[s3cr3t-from-env-9f8a]" in r.get("transcript", ""), str(r))
        before_echo = r.get("transcript", "").split("GOT:")[0]
        check("real value NOT in the pre-echo portion of the transcript", "s3cr3t-from-env-9f8a" not in before_echo, before_echo)
        check("redaction marker present instead", "***" in before_echo, before_echo)
    finally:
        del os.environ["_TEST_INTERACTIVE_RUN_SECRET"]


def check_send_env_missing_var(mod) -> None:
    print("send_env: missing variable fails clearly, before spawning anything")
    assert "_TEST_INTERACTIVE_RUN_DEFINITELY_UNSET" not in os.environ
    r = call(mod, {
        "command": PROMPT_CMD,
        "steps": [{"expect": "Enter: ", "send_env": "_TEST_INTERACTIVE_RUN_DEFINITELY_UNSET"}],
    })
    check("returns an error", "error" in r, str(r))
    check("error names the missing variable", "_TEST_INTERACTIVE_RUN_DEFINITELY_UNSET" in r.get("error", ""), str(r))
    check("no transcript/exit_status leaked through (never spawned)", "transcript" not in r, str(r))


def check_send_file_not_available(mod) -> None:
    print("send_file does not exist as an option -- treated as an unknown "
          "field, resolved as if only send/send_env were considered")
    r = call(mod, {
        "command": PROMPT_CMD,
        "steps": [{"expect": "Enter: ", "send_file": "/tmp/whatever"}],
    })
    check("no send/send_env present -> error, send_file is simply ignored", "error" in r, str(r))
    check("error does not treat send_file as a valid source", "send_file" not in r.get("error", "") or "must include" in r.get("error", ""), str(r))


def check_conflicting_and_missing_sources(mod) -> None:
    print("validation: exactly one of send/send_env required")
    r = call(mod, {
        "command": PROMPT_CMD,
        "steps": [{"expect": "Enter: ", "send": "a", "send_env": "PATH"}],
    })
    check("both send + send_env is an error", "error" in r, str(r))

    r = call(mod, {
        "command": PROMPT_CMD,
        "steps": [{"expect": "Enter: "}],
    })
    check("neither send nor send_env is an error", "error" in r, str(r))


def main() -> int:
    import core.processes as mod

    check_existing_literal_send_unaffected(mod)
    print()
    check_send_env_happy_path(mod)
    print()
    check_send_env_missing_var(mod)
    print()
    check_send_file_not_available(mod)
    print()
    check_conflicting_and_missing_sources(mod)

    total = len(FAILURES)
    print(f"\n{'FAILED' if total else 'all checks passed'}"
          + (f": {total} failure(s)" if total else ""))
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
