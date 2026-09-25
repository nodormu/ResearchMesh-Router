"""Behavioural regression tests for core/processes.py.

    python test_processes.py

Covers `interactive_run`'s `send_env`/`send_secret` step fields — added so a
password/token prompt can be answered without the real value ever having to
be written into the tool call itself (see the module's own docstring for the
full rationale). Spawns real `bash -c 'read -s -p ... ; echo ...'` prompts
via pexpect (through the real `_run()`, not a re-implementation) to confirm
the actual value reaches the child process correctly AND never appears in
the plain, unredacted transcript — not just that the code parses.

`send_secret` shells out to a REAL `pass` binary, but this suite does not
depend on a real GPG key/password-store being set up — a tiny fake `pass`
script (plain shell, no gpg at all) is placed on `PATH` ahead of any real
one for the duration of these specific checks, giving fully deterministic,
fast, CI-safe coverage of `_resolve_reply`'s own logic (found entry, missing
entry, `pass` altogether absent, a hung/unanswerable prompt) without ever
touching real encryption or timing on an actual passphrase cache.
"""

import asyncio
import json
import os
import shlex
import stat
import sys
import tempfile

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


def match_cmd(expected: str) -> str:
    """A prompt whose own script compares the received value against
    `expected` INSIDE the shell, printing only MATCH/MISMATCH — never the
    real value itself. Used for "did the child receive the correct value"
    checks now that redaction correctly scrubs every occurrence of a secret
    (see `check_secret_redacted_even_when_echoed_back_later`): a test that
    verified correctness by echoing the raw value back and inspecting the
    transcript would be checking for something redaction is now supposed to
    remove, which is backwards. `PROMPT_CMD`'s echo-back style is kept
    on purpose for the one test that specifically needs a secret to leak
    into unrelated output, to prove redaction now catches it anyway.
    """
    return (
        f'read -s -p "Enter: " val; '
        f'if [ "$val" = {shlex.quote(expected)} ]; then echo "GOT:MATCH"; '
        f'else echo "GOT:MISMATCH"; fi'
    )


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
        "command": match_cmd("shouldberedacted"),
        "steps": [{"expect": "Enter: ", "send": "shouldberedacted", "secret": True}],
    })
    check("secret:true still redacts the echoed send", "shouldberedacted" not in r.get("transcript", ""), str(r))
    check("child still received the real value despite redaction", "GOT:MATCH" in r.get("transcript", ""), str(r))


def check_send_env_happy_path(mod) -> None:
    print("send_env: real value reaches the child, never appears unredacted in transcript")
    os.environ["_TEST_INTERACTIVE_RUN_SECRET"] = "s3cr3t-from-env-9f8a"
    try:
        r = call(mod, {
            "command": match_cmd("s3cr3t-from-env-9f8a"),
            "steps": [{"expect": "Enter: ", "send_env": "_TEST_INTERACTIVE_RUN_SECRET"}],
        })
        check("no error", "error" not in r, str(r))
        check("child received the real env value", "GOT:MATCH" in r.get("transcript", ""), str(r))
        check("real value NOT anywhere in the transcript", "s3cr3t-from-env-9f8a" not in r.get("transcript", ""), str(r))
        check("redaction marker present instead", "***" in r.get("transcript", ""), str(r))
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


class _FakePassOnPath:
    """Puts a tiny fake `pass` script on `PATH`, ahead of any real one, for
    the duration of a `with` block. Plain shell, zero gpg/pass dependency —
    `show existing-entry` prints a known value, `show sleeps-forever` blocks
    forever (simulating an unanswerable pinentry prompt), anything else
    fails with the same shape of stderr message a real `pass` would give.
    """

    SCRIPT = """#!/bin/sh
if [ "$1" = "show" ]; then
    case "$2" in
        existing-entry) echo "fake-secret-value-9k2m"; exit 0 ;;
        sleeps-forever) sleep 999; exit 0 ;;
        *) echo "Error: $2 is not in the password store." >&2; exit 1 ;;
    esac
fi
exit 1
"""

    def __enter__(self):
        self._tmpdir = tempfile.mkdtemp()
        path = os.path.join(self._tmpdir, "pass")
        with open(path, "w") as f:
            f.write(self.SCRIPT)
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
        self._old_path = os.environ["PATH"]
        os.environ["PATH"] = self._tmpdir + os.pathsep + self._old_path
        return self

    def __exit__(self, *exc):
        os.environ["PATH"] = self._old_path


def check_send_secret_happy_path(mod) -> None:
    print("send_secret: real value reaches the child, never appears unredacted in transcript")
    with _FakePassOnPath():
        r = call(mod, {
            "command": match_cmd("fake-secret-value-9k2m"),
            "steps": [{"expect": "Enter: ", "send_secret": "existing-entry"}],
        })
    check("no error", "error" not in r, str(r))
    check("child received the value pass show printed", "GOT:MATCH" in r.get("transcript", ""), str(r))
    check("real value NOT anywhere in the transcript", "fake-secret-value-9k2m" not in r.get("transcript", ""), str(r))
    check("redaction marker present instead", "***" in r.get("transcript", ""), str(r))


def check_send_secret_missing_entry(mod) -> None:
    print("send_secret: entry not in the store fails clearly, before spawning anything")
    with _FakePassOnPath():
        r = call(mod, {
            "command": PROMPT_CMD,
            "steps": [{"expect": "Enter: ", "send_secret": "no-such-entry"}],
        })
    check("returns an error", "error" in r, str(r))
    check("error surfaces pass's own stderr text", "not in the password store" in r.get("error", ""), str(r))
    check("no transcript/exit_status leaked through (never spawned)", "transcript" not in r, str(r))


def check_send_secret_pass_not_installed(mod) -> None:
    print("send_secret: pass genuinely absent from PATH fails clearly")
    old_path = os.environ["PATH"]
    try:
        os.environ["PATH"] = "/nonexistent-empty-dir"
        r = call(mod, {
            "command": PROMPT_CMD,
            "steps": [{"expect": "Enter: ", "send_secret": "anything"}],
        })
    finally:
        os.environ["PATH"] = old_path
    check("returns an error", "error" in r, str(r))
    check("error says pass isn't installed", "not installed" in r.get("error", ""), str(r))


def check_send_secret_timeout(mod) -> None:
    print("send_secret: an unanswerable prompt times out with a clear "
          "message instead of hanging for the full interactive_run timeout")
    old_timeout = mod._SEND_SECRET_TIMEOUT
    mod._SEND_SECRET_TIMEOUT = 1
    try:
        with _FakePassOnPath():
            r = call(mod, {
                "command": PROMPT_CMD,
                "steps": [{"expect": "Enter: ", "send_secret": "sleeps-forever"}],
            })
    finally:
        mod._SEND_SECRET_TIMEOUT = old_timeout
    check("returns an error", "error" in r, str(r))
    check("error explains the likely cause (unanswerable passphrase prompt)", "passphrase" in r.get("error", ""), str(r))
    check("no transcript leaked through (never spawned)", "transcript" not in r, str(r))


def check_secret_redacted_even_when_echoed_back_later(mod) -> None:
    print("regression: a secret value is scrubbed EVERYWHERE in the "
          "transcript, not just on the line where it was sent -- this is "
          "a real bug that was caught live, not a hypothetical")
    os.environ["_TEST_ECHO_BACK_SECRET"] = "Jum@nji23Suck$2#"
    try:
        r = call(mod, {
            "command": PROMPT_CMD,
            "steps": [{"expect": "Enter: ", "send_env": "_TEST_ECHO_BACK_SECRET"}],
        })
    finally:
        del os.environ["_TEST_ECHO_BACK_SECRET"]
    check("no error", "error" not in r, str(r))
    transcript = r.get("transcript", "")
    check(
        "real value does not appear ANYWHERE, including the child's own later echo",
        "Jum@nji23Suck$2#" not in transcript,
        transcript,
    )
    check("both occurrences (send line AND echoed-back line) show the redaction marker",
          transcript.count("***") == 2, transcript)


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
    print("validation: exactly one of send/send_env/send_secret required")
    r = call(mod, {
        "command": PROMPT_CMD,
        "steps": [{"expect": "Enter: ", "send": "a", "send_env": "PATH"}],
    })
    check("both send + send_env is an error", "error" in r, str(r))

    r = call(mod, {
        "command": PROMPT_CMD,
        "steps": [{"expect": "Enter: ", "send_env": "PATH", "send_secret": "x"}],
    })
    check("both send_env + send_secret is an error", "error" in r, str(r))

    r = call(mod, {
        "command": PROMPT_CMD,
        "steps": [{"expect": "Enter: "}],
    })
    check("neither send nor send_env/send_secret is an error", "error" in r, str(r))


def main() -> int:
    import core.processes as mod

    check_existing_literal_send_unaffected(mod)
    print()
    check_send_env_happy_path(mod)
    print()
    check_send_env_missing_var(mod)
    print()
    check_send_secret_happy_path(mod)
    print()
    check_send_secret_missing_entry(mod)
    print()
    check_send_secret_pass_not_installed(mod)
    print()
    check_send_secret_timeout(mod)
    print()
    check_secret_redacted_even_when_echoed_back_later(mod)
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
