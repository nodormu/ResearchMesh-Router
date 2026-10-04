"""Interactive processes: pexpect, for commands bash cannot run.

The bash tool pipes stdin from nowhere, so anything that asks a question
mid-run (ssh host-key confirmation, a sudo password, fdisk, an installer, a
REPL) blocks until the timeout. This tool spawns the program on a
pseudo-terminal and answers its prompts from a script the model supplies up
front: one tool instead of a spawn/send/expect trio.

A step's answer comes from one of three sources:
- `send`: a literal string the model writes. `secret: true` keeps it out of the
returned transcript, but the value still passes through the model.
- `send_env`: the NAME of an environment variable, read here and never by the
model. Always treated as secret.
- `send_secret`: the NAME of a `pass` entry, resolved locally with `pass show`.
The value is GPG-encrypted at rest and needs no GUI, D-Bus session or desktop,
so it works on a headless server. `secret-tool`/libsecret was rejected because
it needs a Secret Service daemon tied to a graphical session.

There is deliberately no file-based source: it would put the secret in a
plaintext file. An env var cannot be policed either (shell history, startup
files), but it does not require a new plaintext artifact.

Requires:  pip install pexpect
"""

import asyncio
import base64
import contextlib
import html
import json
import os
import re
import signal
import subprocess
from pathlib import Path
from urllib.parse import quote, quote_plus

from core.claude_learned_schemas import SHELL_EXECUTABLE, apply_shell_prelude
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
            "waits for a regex to appear, then sends a line. For a password or "
            "token specifically, use a step's `send_env`/`send_secret` "
            "instead of `send` — the real value is read locally (from an "
            "environment variable, or from a `pass` password-store entry for "
            "genuine at-rest encryption) and never has to be written into "
            "this call at all, unlike a literal `send`, which the user has "
            "to tell you and you have to write down to use. Returns JSON "
            "with the terminal transcript and the exit status."
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
                                "description": (
                                    "Literal line to send once it matches. "
                                    "Exactly one of `send`/`send_env`/"
                                    "`send_secret` is required per step. Use "
                                    "this for non-secret input (a username, "
                                    "'yes', a menu choice) — for a password "
                                    "or token, use `send_env`/`send_secret` "
                                    "instead so the real value never has to "
                                    "be written into this call at all."
                                ),
                            },
                            "send_env": {
                                "type": "string",
                                "description": (
                                    "Name of an environment variable — its "
                                    "value is read locally and sent, never "
                                    "written into this tool call. Use for a "
                                    "password/token/passphrase: the user "
                                    "sets the variable in their own shell "
                                    "ahead of time (out of band, never told "
                                    "to you), you only ever name it here. "
                                    "Always treated as secret in the "
                                    "returned transcript, regardless of the "
                                    "`secret` field. Errors clearly if the "
                                    "named variable isn't set, before "
                                    "spawning anything."
                                ),
                            },
                            "send_secret": {
                                "type": "string",
                                "description": (
                                    "Name of a `pass` (the standard unix "
                                    "password manager) entry — resolved "
                                    "locally via `pass show <name>` and "
                                    "sent, never written into this tool "
                                    "call. Prefer this over `send_env` when "
                                    "genuine at-rest encryption matters: a "
                                    "`pass` entry is GPG-encrypted on disk, "
                                    "unlike an env var, which is only "
                                    "plaintext-free if the user is careful "
                                    "about how they set it. Needs no "
                                    "desktop environment or GUI — works "
                                    "identically on a headless server. "
                                    "Always treated as secret in the "
                                    "returned transcript, regardless of the "
                                    "`secret` field. Errors clearly if "
                                    "`pass`/the named entry isn't available, "
                                    "before spawning anything. Don't know "
                                    "which entry to use? Pass the literal "
                                    "string \"?\" instead of a real name — "
                                    "this returns a fixed, ready-to-relay "
                                    "prompt built from the real vault "
                                    "contents (\"please select the cred "
                                    "name I need to use:\" plus every real "
                                    "entry), not something to guess at or "
                                    "compose yourself."
                                ),
                            },
                            "secret": {
                                "type": "boolean",
                                "description": (
                                    "Redact this step's `send` response from "
                                    "the returned transcript. Only meaningful "
                                    "for a literal `send` — `send_env`/"
                                    "`send_secret` are always redacted "
                                    "unconditionally, since their entire "
                                    "point is keeping the real value out of "
                                    "anything you see."
                                ),
                            },
                        },
                        "required": ["expect"],
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
# Timeout for `send_secret`'s `pass show` subprocess. A module attribute, not a
# default argument, so test_processes.py can shrink it. See `_resolve_reply`
# for why it exists.
_SEND_SECRET_TIMEOUT = 30

# Vault entry names the user has typed in their own messages this session
# (see `note_user_message`). Only these can be decrypted. Module-level so it
# persists across separate tool calls in the same process.
_confirmed_secret_entries: set[str] = set()


def handles(name: str) -> bool:
    return name in _TOOL_NAMES


async def execute(name: str, tool_input: dict) -> str:
    if name != "interactive_run":
        return json.dumps({"error": f"unknown process tool {name!r}"})
    return await asyncio.to_thread(_run, tool_input)


def _resolve_reply(step: dict) -> tuple[str, bool, str | None]:
    """Return (reply_text, is_secret, error) for one step.

    Exactly one of `send`, `send_env` or `send_secret` must be present;
    anything else is returned as `error`. `send_env` and `send_secret` always
    return `is_secret=True`; a literal `send` follows the step's `secret` field
    (default False).

    `send_secret` runs `pass show <name>` with a bounded timeout
    (`_SEND_SECRET_TIMEOUT`, 30s). If the GPG key is not unlocked in
    `gpg-agent`, a GUI `pinentry` can appear on the user's screen and answering
    it takes human time; with no terminal here, a text pinentry would hang for
    the full `interactive_run` timeout. The error says so: unlock the key once
    in a real terminal, which caches it in `gpg-agent`.
    """
    sources = [k for k in ("send", "send_env", "send_secret") if k in step]
    if len(sources) == 0:
        return "", False, "must include one of `send`, `send_env`, `send_secret`"
    if len(sources) > 1:
        return "", False, f"only one of `send`/`send_env`/`send_secret` allowed, got {sources}"

    if "send" in step:
        return str(step["send"]), bool(step.get("secret")), None

    if "send_env" in step:
        var_name = str(step["send_env"])
        value = os.environ.get(var_name)
        if value is None:
            return "", False, f"environment variable {var_name!r} is not set"
        return value, True, None

    value, error = resolve_secret(str(step["send_secret"]))
    if error:
        return "", False, error
    return value, True, None


def _pass_show(entry_name: str) -> subprocess.CompletedProcess:
    """`pass show <name>` in its own process group. On timeout the whole group is
    killed: `subprocess.run` would kill only `pass`, and the `gpg` it started
    would outlive it."""
    proc = subprocess.Popen(
        ["pass", "show", entry_name],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=_SEND_SECRET_TIMEOUT)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.communicate(timeout=5)
        raise
    return subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)


def resolve_secret(entry_name: str) -> tuple[str, str | None]:
    """Return (value, error) for a `pass` entry name. Shared by every tool that
    accepts a vault entry (`interactive_run` `send_secret`, `browser_fill`
    `value_secret`), so they share one name check and one decrypt path.

    Decrypts only an entry the user typed in one of their own messages this
    session (`note_user_message`). Any other name, or "?", returns the
    selection prompt as the error and decrypts nothing; naming the entry
    again from the model side does not change that.
    """
    if entry_name == "?" or entry_name not in _confirmed_secret_entries:
        return "", _select_entry_prompt()

    try:
        result = _pass_show(entry_name)
    except FileNotFoundError:
        return "", "`pass` is not installed (see the module docstring for setup)"
    except subprocess.TimeoutExpired:
        return "", (
            f"`pass show {entry_name!r}` did not return within "
            f"{_SEND_SECRET_TIMEOUT}s — likely waiting on a GPG passphrase "
            "prompt with no terminal here to answer it. Unlock the key "
            "once, manually, in your own terminal (`pass show ...` there) "
            "first — gpg-agent caches it for a while afterward, and this "
            "call will then succeed silently."
        )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
        return "", f"`pass show {entry_name!r}` failed: {detail}{_available_entries_hint()}"
    first_line = result.stdout.splitlines()[0] if result.stdout else ""
    return first_line, None


def _store_dir() -> Path:
    return Path(os.environ.get("PASSWORD_STORE_DIR", "~/.password-store")).expanduser()


def _store_entries(store_dir: Path) -> list[str]:
    """Entry names in the store, read from the `*.gpg` filenames. Nothing is
    decrypted."""
    if not store_dir.is_dir():
        return []
    return sorted(
        str(p.relative_to(store_dir))[: -len(".gpg")]
        for p in store_dir.rglob("*.gpg")
    )


def note_user_message(text: str) -> None:
    """Record every vault entry whose name appears in `text`. Called with the
    user's own message only, so a name the model picked cannot confirm itself."""
    for name in _store_entries(_store_dir()):
        if re.search(rf"(?<![\w/-]){re.escape(name)}(?![\w/-])", text):
            _confirmed_secret_entries.add(name)


def _select_entry_prompt() -> str:
    """The fixed text returned for `"send_secret": "?"`, the caller's way of
    saying it needs a credential and does not know which vault entry.

    Built in code from the real entries, so neither the list nor the wording of
    the request is left to the model. Reads `$PASSWORD_STORE_DIR` (default
    `~/.password-store`) and walks it for `*.gpg` files instead of parsing
    `pass ls` output, whose tree drawing loses the folder of a nested entry
    (`aws/prod` becomes `prod`). Nothing is decrypted: filenames are read,
    files are never opened.
    """
    store_dir = _store_dir()
    if not store_dir.is_dir():
        return f"no password store found at {store_dir} — nothing to select"
    entries = _store_entries(store_dir)
    if not entries:
        return "the password store is empty — nothing to select"
    return "please select the cred name I need to use:\n" + "\n".join(entries)


def _available_entries_hint() -> str:
    """Appended to a `send_secret` failure: the entry NAMES currently in the
    vault, never values (`pass ls` lists directories and needs no
    passphrase). A wrong name then fails with what exists, without another
    round trip. It is not a license to pick an entry by guessing: the user
    names the entry. Best-effort: if `pass ls` fails, the base error is
    returned alone.
    """
    try:
        result = subprocess.run(
            ["pass", "ls"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:
        return ""
    if result.returncode != 0 or not result.stdout.strip():
        return ""
    return f"\n\nEntries actually in the vault:\n{result.stdout.strip()}"


def _b64_fragments(raw: bytes, encode) -> set[str]:
    """The base64 characters that depend only on `raw`'s bytes, at each of the
    three alignments `raw` can take inside a longer string (`user:` + secret in
    a Basic-auth header lands at an arbitrary offset). Characters that also
    depend on a neighbouring byte are left out, so every fragment matches
    wherever the secret sits."""
    out = set()
    for k in range(3):
        enc = encode(b"\x00" * k + raw).decode().rstrip("=")
        out.add(enc[-(-8 * k // 6) : (8 * (k + len(raw))) // 6])
    return out


def _secret_forms(value: str) -> set[str]:
    """`value` plus the encodings a program or page commonly echoes it in:
    percent-encoded (both hex cases), form-encoded, HTML-escaped, JSON-escaped,
    hex, and base64 (standard, URL-safe, padded or not, at any alignment).
    Derived forms shorter than 6 characters are dropped: they would match
    ordinary text."""
    if not value:
        return set()
    raw = value.encode()
    forms = {
        quote(value, safe=""),
        quote(value),
        quote_plus(value),
        html.escape(value),
        html.escape(value, quote=False),
        json.dumps(value)[1:-1],
        json.dumps(value, ensure_ascii=False)[1:-1],
        raw.hex(),
        base64.b64encode(raw).decode(),
        base64.urlsafe_b64encode(raw).decode(),
    }
    forms |= {f.rstrip("=") for f in forms}
    forms |= {re.sub(r"%[0-9A-F]{2}", lambda m: m.group().lower(), f) for f in forms}
    if len(value) >= 8:
        forms |= _b64_fragments(raw, base64.b64encode)
        forms |= _b64_fragments(raw, base64.urlsafe_b64encode)
    return {f for f in forms if len(f) >= 6} | {value}


def _redact(transcript: str, secret_values: list[str]) -> str:
    """Replace every occurrence of every value in `secret_values`, and of each
    encoded form from `_secret_forms`, anywhere in `transcript` with `***`.

    Scrubbing the complete text, not just the line where a step sent a secret,
    covers a child process or page that repeats the value on its own later.
    Longest forms go first so a long form is never left half-replaced by a
    shorter one nested inside it. Empty values are skipped.
    """
    forms: set[str] = set()
    for value in set(secret_values):
        forms |= _secret_forms(value)
    for form in sorted(forms, key=len, reverse=True):
        transcript = transcript.replace(form, "***")
    return transcript


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

    # Resolve every step's reply text, and whether it is secret, before
    # spawning: a missing env var or unreadable entry is a configuration error
    # to report up front, not after a program was left mid-prompt (ssh
    # half-connected, an installer half-run).
    resolved_replies: list[tuple[str, bool]] = []
    for i, step in enumerate(steps):
        reply, is_secret, error = _resolve_reply(step)
        if error:
            return json.dumps({"error": f"step {i}: {error}"})
        resolved_replies.append((reply, is_secret))

    transcript: list[str] = []
    matched = 0
    timed_out_at = None

    try:
        child = pexpect.spawn(
            SHELL_EXECUTABLE,
            ["-c", apply_shell_prelude(command)],
            encoding="utf-8",
            codec_errors="replace",
            timeout=timeout,
            echo=False,
        )
    except Exception as e:
        return json.dumps({"error": f"could not spawn command: {e}"})

    try:
        for step, (reply, is_secret) in zip(steps, resolved_replies, strict=True):
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
            child.sendline(reply)
            # Appended as the real value, never "***", even when `is_secret`.
            # Redaction happens once, over the complete transcript, via
            # `_redact()`; redacting only here missed a secret the child later
            # echoed on its own.
            transcript.append(reply + "\n")
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

    full_transcript = _redact(
        "".join(transcript),
        secret_values=[reply for reply, is_secret in resolved_replies if is_secret],
    )

    return json.dumps(
        {
            "transcript": clip(full_transcript, _MAX_TRANSCRIPT),
            "steps_matched": matched,
            "steps_total": len(steps),
            "exit_status": child.exitstatus,
            "signal_status": child.signalstatus,
            "timed_out_waiting_for": timed_out_at,
        }
    )
