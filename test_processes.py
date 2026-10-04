"""Behavioural regression tests for core/processes.py.

    python test_processes.py

Covers the `send_env` and `send_secret` step fields of `interactive_run`: the
real value reaches the child (real `bash -c 'read -s ...'` prompts through the
real `_run()`) and never appears in the unredacted transcript.

`send_secret` normally shells out to `pass`; a small fake `pass` script (plain
shell, no gpg) is put on `PATH` ahead of any real one, so the checks of
`_resolve_reply` (found entry, missing entry, no `pass`, an unanswerable
prompt) are deterministic and need no GPG key or password store.
"""

import asyncio
import json
import os
import shlex
import signal
import stat
import sys
import tempfile
import time

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
    """A prompt whose script compares the received value to `expected` inside
    the shell and prints only MATCH or MISMATCH, never the value. Used for
    "did the child receive the right value" checks, since redaction now
    scrubs every occurrence of a secret and echoing it back would test the
    wrong thing. `PROMPT_CMD`'s echo-back style stays for the one test that
    needs a secret to leak into unrelated output, to prove redaction catches
    it.
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
    """Put a small fake `pass` on `PATH`, ahead of any real one, for the
    duration of a `with` block. Plain shell, no gpg: `show existing-entry`
    prints a known value, `show sleeps-forever` blocks (an unanswerable
    pinentry), anything else fails with a real `pass`-style stderr message.
    """

    SCRIPT = """#!/bin/sh
if [ "$1" = "show" ]; then
    case "$2" in
        existing-entry) echo "fake-secret-value-9k2m"; exit 0 ;;
        fresh-unconfirmed-entry) echo "fake-secret-value-9k2m"; exit 0 ;;
        sleeps-forever) sleep 999 & echo $! > "$FAKE_PASS_CHILD_PIDFILE"; wait ;;
        *) echo "Error: $2 is not in the password store." >&2; exit 1 ;;
    esac
fi
if [ "$1" = "ls" ]; then
    echo "Password Store"
    echo "existing-entry"
    exit 0
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


def confirm(mod, *names: str) -> None:
    """Mark entry names as typed by the user, so tests of other behaviour get
    past the name gate. `check_entry_needs_user_typed_name`
    tests the gate itself.
    """
    mod._confirmed_secret_entries.update(names)


def check_send_secret_happy_path(mod) -> None:
    print("send_secret: real value reaches the child, never appears unredacted in transcript")
    confirm(mod, "existing-entry")
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
    confirm(mod, "no-such-entry")
    with _FakePassOnPath():
        r = call(mod, {
            "command": PROMPT_CMD,
            "steps": [{"expect": "Enter: ", "send_secret": "no-such-entry"}],
        })
    check("returns an error", "error" in r, str(r))
    check("error surfaces pass's own stderr text", "not in the password store" in r.get("error", ""), str(r))
    check("no transcript/exit_status leaked through (never spawned)", "transcript" not in r, str(r))


def check_send_secret_missing_entry_shows_real_available_entries(mod) -> None:
    print("send_secret: a wrong/guessed entry name's error includes the "
          "REAL list of what's actually in the vault (pass ls), so a "
          "hallucinated or mistyped name doesn't just fail blind")
    confirm(mod, "totally-made-up-name")
    with _FakePassOnPath():
        r = call(mod, {
            "command": PROMPT_CMD,
            "steps": [{"expect": "Enter: ", "send_secret": "totally-made-up-name"}],
        })
    check("returns an error", "error" in r, str(r))
    check("error includes the real available entry name", "existing-entry" in r.get("error", ""), str(r))
    check("error is clearly labeled as the real vault contents", "Entries actually in the vault" in r.get("error", ""), str(r))


def check_send_secret_pass_not_installed(mod) -> None:
    print("send_secret: pass genuinely absent from PATH fails clearly")
    confirm(mod, "anything")
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
    confirm(mod, "sleeps-forever")
    old_timeout = mod._SEND_SECRET_TIMEOUT
    mod._SEND_SECRET_TIMEOUT = 1
    pidfile = os.path.join(tempfile.mkdtemp(), "child.pid")
    os.environ["FAKE_PASS_CHILD_PIDFILE"] = pidfile
    try:
        with _FakePassOnPath():
            r = call(mod, {
                "command": PROMPT_CMD,
                "steps": [{"expect": "Enter: ", "send_secret": "sleeps-forever"}],
            })
    finally:
        mod._SEND_SECRET_TIMEOUT = old_timeout
        del os.environ["FAKE_PASS_CHILD_PIDFILE"]
    # The fake `pass` started a child, as the real one starts gpg. Killing only
    # `pass` would leave it running.
    with open(pidfile) as f:
        child = int(f.read())
    alive = True
    for _ in range(20):
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            alive = False
            break
        time.sleep(0.1)
    if alive:
        os.kill(child, signal.SIGKILL)
    check("the timed-out `pass` leaves no child process behind", not alive, f"pid {child}")
    check("returns an error", "error" in r, str(r))
    check("error explains the likely cause (unanswerable passphrase prompt)", "passphrase" in r.get("error", ""), str(r))
    check("no transcript leaked through (never spawned)", "transcript" not in r, str(r))


def check_secret_redacted_even_when_echoed_back_later(mod) -> None:
    print("a secret value is scrubbed everywhere in the transcript, not just "
          "on the line where it was sent")
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


def check_entry_needs_user_typed_name(mod) -> None:
    print("send_secret: an entry decrypts only after the USER typed its name; "
          "a real name chosen by the model is refused, however often it is "
          "repeated")
    # _select_entry_prompt() reads $PASSWORD_STORE_DIR directly, independent
    # of the faked `pass` binary _FakePassOnPath sets up -- give it its own
    # controlled store so this test doesn't depend on whatever vault state
    # happens to exist on the machine actually running it.
    store = tempfile.mkdtemp()
    old_dir = os.environ.get("PASSWORD_STORE_DIR")
    mod._confirmed_secret_entries.discard("fresh-unconfirmed-entry")
    try:
        open(os.path.join(store, "fresh-unconfirmed-entry.gpg"), "w").close()
        os.environ["PASSWORD_STORE_DIR"] = store

        step = {"expect": "Enter: ", "send_secret": "fresh-unconfirmed-entry"}
        with _FakePassOnPath():
            r1 = call(mod, {"command": PROMPT_CMD, "steps": [step]})
            r1b = call(mod, {"command": PROMPT_CMD, "steps": [step]})
        check("an untyped name is refused, though it is a real correct name",
              "error" in r1, str(r1))
        check("refusal is the exact same selection prompt \"?\" produces",
              "please select the cred name I need to use:" in r1.get("error", ""), str(r1))
        check("refusal names the real entry, from the real store",
              "fresh-unconfirmed-entry" in r1.get("error", ""), str(r1))
        check("no transcript leaked through on the refused attempt",
              "transcript" not in r1, str(r1))
        check("repeating the same name is still refused",
              "please select the cred name I need to use:" in r1b.get("error", ""), str(r1b))

        mod.note_user_message("use fresh-unconfirmed-entry-2 for this")
        with _FakePassOnPath():
            r_near = call(mod, {"command": PROMPT_CMD, "steps": [step]})
        check("a message containing only a longer name does not confirm it",
              "error" in r_near, str(r_near))

        mod.note_user_message("ok, fresh-unconfirmed-entry")
        with _FakePassOnPath():
            r2 = call(mod, {
                "command": match_cmd("fake-secret-value-9k2m"),
                "steps": [step],
            })
        check("after the user types the name it proceeds",
              "error" not in r2, str(r2))
        check("and actually works correctly once confirmed",
              "GOT:MATCH" in r2.get("transcript", ""), str(r2))
    finally:
        if old_dir is None:
            os.environ.pop("PASSWORD_STORE_DIR", None)
        else:
            os.environ["PASSWORD_STORE_DIR"] = old_dir
        import shutil
        shutil.rmtree(store, ignore_errors=True)


def check_send_secret_select_sentinel(mod) -> None:
    print("send_secret: \"?\" returns the exact hardcoded selection prompt, "
          "built from REAL vault entries -- not something composed on the "
          "fly, and correct for nested (folder/entry) paths specifically")
    store = tempfile.mkdtemp()
    old_dir = os.environ.get("PASSWORD_STORE_DIR")
    try:
        os.makedirs(os.path.join(store, "aws"))
        open(os.path.join(store, "github.gpg"), "w").close()
        open(os.path.join(store, "aws", "prod.gpg"), "w").close()
        os.environ["PASSWORD_STORE_DIR"] = store
        r = call(mod, {
            "command": PROMPT_CMD,
            "steps": [{"expect": "Enter: ", "send_secret": "?"}],
        })
        err = r.get("error", "")
        check("returns an error (step never proceeds)", "error" in r, str(r))
        check("exact opening line present", "please select the cred name I need to use:" in err, err)
        check("flat entry present by plain name", "github" in err, err)
        check("nested entry present with its FULL path, not just the leaf", "aws/prod" in err, err)
        check("no transcript leaked through (never spawned)", "transcript" not in r, str(r))
    finally:
        if old_dir is None:
            os.environ.pop("PASSWORD_STORE_DIR", None)
        else:
            os.environ["PASSWORD_STORE_DIR"] = old_dir
        import shutil
        shutil.rmtree(store, ignore_errors=True)


def check_send_secret_select_sentinel_empty_store(mod) -> None:
    print("send_secret: \"?\" against an empty/nonexistent store says so plainly")
    old_dir = os.environ.get("PASSWORD_STORE_DIR")
    try:
        os.environ["PASSWORD_STORE_DIR"] = "/tmp/definitely-does-not-exist-store"
        r = call(mod, {
            "command": PROMPT_CMD,
            "steps": [{"expect": "Enter: ", "send_secret": "?"}],
        })
        check("returns an error", "error" in r, str(r))
        check("says no store found, not a confusing crash", "no password store found" in r.get("error", ""), str(r))
    finally:
        if old_dir is None:
            os.environ.pop("PASSWORD_STORE_DIR", None)
        else:
            os.environ["PASSWORD_STORE_DIR"] = old_dir


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
    check_entry_needs_user_typed_name(mod)
    print()
    check_send_secret_happy_path(mod)
    print()
    check_send_secret_missing_entry(mod)
    print()
    check_send_secret_missing_entry_shows_real_available_entries(mod)
    print()
    check_send_secret_select_sentinel(mod)
    print()
    check_send_secret_select_sentinel_empty_store(mod)
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
