import asyncio
import base64
import os
import shutil
import subprocess
import tomllib
from pathlib import Path

from core.output import IMAGE_MEDIA_TYPES, clip, image_result

# Anthropic-defined ("learned") tool schemas. Claude already knows how to use
# these, so they carry no description.
#   bash + text_editor      -> client-executed (we run them below)
#   web_search + web_fetch  -> server-executed by Anthropic (no executor here)

BASH_TOOL = {"type": "bash_20250124", "name": "bash"}
TEXT_EDITOR_TOOL = {
    "type": "text_editor_20250728",
    "name": "str_replace_based_edit_tool",
}
# The web tools are versioned by capability, not superseded: each dated variant
# is a superset of the last, so we track the newest. 20260318 adds an optional
# `response_inclusion` to both (set it to "excluded" to drop dynamically-filtered
# result blocks from the response); web_fetch also carries `use_cache` from
# 20260309. Both are left at their defaults ("full" / true) here.
WEB_SEARCH_TOOL = {"type": "web_search_20260318", "name": "web_search"}
WEB_FETCH_TOOL = {"type": "web_fetch_20260318", "name": "web_fetch"}

TOOLS = [BASH_TOOL, TEXT_EDITOR_TOOL, WEB_SEARCH_TOOL, WEB_FETCH_TOOL]

# Tool names we execute locally. web_search / web_fetch run on Anthropic's side
# and never come back to us as tool_use blocks.
_LOCAL = {"bash", "str_replace_based_edit_tool"}

_MAX_OUTPUT = 12000

# --- bash tool shell selection -------------------------------------------
# Ubuntu (confirmed live) and Debian (documented policy since Squeeze) point
# /bin/sh at dash, not bash, so Python's shell=True default would silently
# run commands under dash's stricter POSIX semantics instead of bash's.
# Other Debian-derived distros likely inherit this, but that's an inference
# from packaging lineage, not something verified here -- see SH_TARGET below
# for how this is actually checked live rather than assumed per distro.
# [bash].shell in config.toml lets the user pin whatever shell they actually
# want (defaulting to /bin/bash, which is also a harmless no-op on distros
# where /bin/sh is already bash, e.g. RHEL/Fedora/AlmaLinux). Resolved once
# at import time — not per call — so it can never drift from the shell name
# baked into SYSTEM_PROMPT in core/chat.py, which reads this same constant.
#
# Naming note: this is deliberately NOT called BASH_SHELL. Anthropic's
# bash_20250124 tool schema requires its `name` field to be the literal
# string "bash" (see BASH_TOOL below) -- that's a fixed, external API
# contract, unrelated to which local interpreter actually executes the
# commands it hands us. A constant called BASH_SHELL sitting next to
# BASH_TOOL could read as if it must also always mean bash, and could
# genuinely hold a zsh/dash path -- SHELL_EXECUTABLE keeps those two
# separate concepts from bleeding into each other for whoever (or
# whatever future coding session) reads this file next.
_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.toml"
_DEFAULT_SHELL = "/bin/bash"


def _resolve_shell_executable() -> str:
    try:
        with open(_CONFIG_PATH, "rb") as f:
            configured = tomllib.load(f).get("bash", {}).get("shell", "")
    except (FileNotFoundError, tomllib.TOMLDecodeError):
        configured = ""

    configured = (configured or "").strip()
    if not configured:
        return _DEFAULT_SHELL

    # Accept either an absolute path or a bare name resolved via $PATH.
    resolved = configured if Path(configured).is_absolute() else shutil.which(configured)
    if resolved and Path(resolved).is_file():
        return resolved

    # A bad/typo'd config value should never silently break the primary
    # bash tool — fall back rather than raise.
    return _DEFAULT_SHELL


SHELL_EXECUTABLE = _resolve_shell_executable()


def _resolve_sh_target() -> str:
    """What /bin/sh actually points to on THIS machine, checked live.

    Never hardcode this as a string literal in source/prompt text -- it
    varies by distro (dash on Debian/Ubuntu, bash on many others) and a
    literal baked in at write-time would ship a stale, potentially false
    claim to every future clone of this repo running on a different box.
    Recomputed fresh every process start so it's always accurate for
    wherever this code actually happens to be running.
    """
    try:
        return os.path.realpath("/bin/sh")
    except OSError:
        return "(unknown -- /bin/sh not found)"


SH_TARGET = _resolve_sh_target()


def handles(name: str) -> bool:
    return name in _LOCAL


async def execute(name: str, tool_input: dict) -> str:
    # Run blocking work off the event loop.
    if name == "bash":
        return await asyncio.to_thread(_run_bash, tool_input)
    if name == "str_replace_based_edit_tool":
        return await asyncio.to_thread(_run_text_editor, tool_input)
    return f"Error: {name} is not locally executable"


# --- bash ---------------------------------------------------------------
# Note: each call is a fresh subprocess, so shell state (cwd, env, variables)
# does NOT persist between calls. Chain with `&&` / `cd x && ...` when needed.
def _run_bash(tool_input: dict) -> str:
    if tool_input.get("restart"):
        return "bash tool restarted"
    command = tool_input.get("command", "")
    if not command:
        return "Error: no command provided"
    try:
        result = subprocess.run(
            command,
            shell=True,
            executable=SHELL_EXECUTABLE,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return "Error: command timed out after 120s"
    except Exception as e:
        return f"Error running command: {e}"

    out = result.stdout or ""
    if result.stderr:
        out += ("\n" if out else "") + result.stderr
    if not out:
        out = f"(no output; exit code {result.returncode})"
    return clip(out, _MAX_OUTPUT)


# --- text editor --------------------------------------------------------
def _run_text_editor(tool_input: dict) -> str:
    command = tool_input.get("command")
    path = tool_input.get("path", "")
    try:
        if command == "view":
            return _view(path, tool_input.get("view_range"))
        if command == "create":
            Path(path).write_text(tool_input.get("file_text", ""), encoding="utf-8")
            return f"File created: {path}"
        if command == "str_replace":
            return _str_replace(
                path, tool_input.get("old_str", ""), tool_input.get("new_str", "")
            )
        if command == "insert":
            return _insert(
                path,
                int(tool_input.get("insert_line", 0)),
                tool_input.get("insert_text", ""),
            )
        return f"Error: unknown text_editor command {command!r}"
    except FileNotFoundError:
        return f"Error: no such file: {path}"
    except Exception as e:
        return f"Error: {e}"


def _view(path: str, view_range):
    p = Path(path)
    if p.is_dir():
        return "\n".join(sorted(x.name for x in p.iterdir())) or "(empty directory)"

    media_type = IMAGE_MEDIA_TYPES.get(p.suffix.lower())
    if media_type is not None:
        if not p.is_file():
            return f"Error: no such file: {path}"
        data = base64.standard_b64encode(p.read_bytes()).decode("ascii")
        return image_result(media_type, data, f"Displayed image: {p}")

    lines = p.read_text(encoding="utf-8").splitlines()
    start, end = 1, len(lines)
    if view_range:
        start, end = view_range[0], view_range[1]
        if end == -1:
            end = len(lines)
    start = max(start, 1)
    end = min(end, len(lines))
    numbered = [f"{i}\t{lines[i - 1]}" for i in range(start, end + 1)]
    return clip("\n".join(numbered), _MAX_OUTPUT) or "(empty file)"


def _str_replace(path: str, old: str, new: str) -> str:
    p = Path(path)
    content = p.read_text(encoding="utf-8")
    count = content.count(old)
    if count == 0:
        return "Error: old_str not found; no changes made"
    if count > 1:
        return f"Error: old_str matched {count} times; it must match exactly once"
    p.write_text(content.replace(old, new, 1), encoding="utf-8")
    return f"Edited {path}"


def _insert(path: str, line: int, text: str) -> str:
    p = Path(path)
    lines = p.read_text(encoding="utf-8").splitlines()
    if line < 0 or line > len(lines):
        return f"Error: insert_line {line} out of range (0..{len(lines)})"
    lines.insert(line, text)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return f"Inserted text after line {line} in {path}"
