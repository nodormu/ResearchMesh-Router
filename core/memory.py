"""`memory` — Anthropic's client-executed memory tool (`memory_20250818`).

A learned schema: Claude already knows the six commands (view, create,
str_replace, insert, delete, rename), so there is no description to write. What
this module supplies is the storage behind them.

`/memories` is a *virtual* prefix, not a real path. Every command is mapped onto
one real directory (`CLAUDE_MEMORY_DIR`, default `./memories`) and confined to
it — that confinement is the one hard requirement Anthropic's docs place on the
client, because a path like `/memories/../../.ssh/id_rsa` is otherwise a read of
the user's private key. `_resolve` therefore canonicalises before checking, so
`..` segments and symlinks that escape the root are both caught.

This is what makes the tool worth a slot: it is the only state here that
survives process exit. The `python` kernel, the browser page, and the DuckDB
connection are all per-session; memory is not. When the tool is in the request,
the API automatically prepends its own memory protocol to the system prompt, so
core/chat.py deliberately says nothing about it.

The return strings match the reference behaviour in Anthropic's docs. Claude was
trained against that wording, so they are copy-sensitive: reword them and the
model starts misreading ordinary outcomes as failures.
"""

import asyncio
import base64
import os
import shutil
from pathlib import Path

from core.output import IMAGE_MEDIA_TYPES, clip, image_result

MEMORY_TOOL = {"type": "memory_20250818", "name": "memory"}
TOOLS = [MEMORY_TOOL]

_URI_ROOT = "/memories"
# The tool's own description promises Claude that text views truncate at 16000
# characters and that it can page through the rest with view_range.
_MAX_VIEW = 16000
_MAX_LINES = 999_999


def _root() -> Path:
    """The real directory `/memories` maps onto. Read per call rather than at
    import so tests and env changes take effect without a reimport."""
    root = Path(os.getenv("CLAUDE_MEMORY_DIR", "memories")).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _resolve(uri: str) -> Path:
    """Map a `/memories/...` URI to a real path, or raise ValueError.

    Canonicalise first, then test containment: `Path.resolve()` collapses `..`
    and follows symlinks, so both traversal attempts and a symlink planted
    inside the tree fail the `is_relative_to` check rather than slipping past a
    substring test.
    """
    if not isinstance(uri, str) or not uri.strip():
        raise ValueError("Error: a path is required")
    uri = uri.strip()
    if uri != _URI_ROOT and not uri.startswith(_URI_ROOT + "/"):
        raise ValueError(
            f"Error: path must start with {_URI_ROOT}. Got: {uri}"
        )

    root = _root()
    relative = uri[len(_URI_ROOT):].lstrip("/")
    target = (root / relative).resolve() if relative else root
    if target != root and not target.is_relative_to(root):
        raise ValueError(f"Error: path escapes {_URI_ROOT}: {uri}")
    return target


def _uri(path: Path) -> str:
    """Real path -> the `/memories/...` URI Claude should see echoed back."""
    root = _root()
    if path == root:
        return _URI_ROOT
    return f"{_URI_ROOT}/{path.relative_to(root).as_posix()}"


def _human_size(num: float) -> str:
    for unit in ("", "K", "M", "G"):
        if num < 1024 or unit == "G":
            return f"{num:.1f}{unit}".replace(".0", "", 1)
        num /= 1024
    return f"{num:.1f}G"


def _numbered(lines: list[str], start: int = 1) -> str:
    # 6-wide, right-aligned, tab separator, 1-indexed — the exact shape the
    # tool description tells Claude to expect.
    return "\n".join(
        f"{i:6d}\t{line}" for i, line in enumerate(lines, start=start)
    )


def handles(name: str) -> bool:
    return name == "memory"


async def execute(name: str, tool_input: dict) -> str | dict:
    if name != "memory":
        return f"Error: {name} is not handled by the memory tool"
    return await asyncio.to_thread(_run, tool_input)


def _run(tool_input: dict) -> str | dict:
    command = tool_input.get("command")
    try:
        if command == "view":
            return _view(tool_input.get("path", ""), tool_input.get("view_range"))
        if command == "create":
            return _create(tool_input.get("path", ""), tool_input.get("file_text", ""))
        if command == "str_replace":
            return _str_replace(
                tool_input.get("path", ""),
                tool_input.get("old_str", ""),
                tool_input.get("new_str") or "",
            )
        if command == "insert":
            return _insert(
                tool_input.get("path", ""),
                int(tool_input.get("insert_line", 0)),
                tool_input.get("insert_text", ""),
            )
        if command == "delete":
            return _delete(tool_input.get("path", ""))
        if command == "rename":
            return _rename(
                tool_input.get("old_path", ""), tool_input.get("new_path", "")
            )
        return f"Error: unknown memory command {command!r}"
    except ValueError as e:
        return str(e)
    except OSError as e:
        return f"Error: {e}"


def _view(uri: str, view_range):
    path = _resolve(uri)
    if not path.exists():
        return f"The path {uri} does not exist. Please provide a valid path."

    if path.is_dir():
        return _view_dir(path, uri)

    media_type = IMAGE_MEDIA_TYPES.get(path.suffix.lower())
    if media_type is not None:
        data = base64.standard_b64encode(path.read_bytes()).decode("ascii")
        return image_result(media_type, data, f"Displayed image: {uri}")

    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return f"Error: {uri} is not a UTF-8 text file"

    lines = text.splitlines()
    if len(lines) > _MAX_LINES:
        return f"File {uri} exceeds maximum line limit of {_MAX_LINES:,} lines."

    start = 1
    if view_range:
        start, end = int(view_range[0]), int(view_range[1])
        if end == -1:
            end = len(lines)
        start = max(start, 1)
        end = min(end, len(lines))
        if start > end:
            return (
                f"Error: Invalid `view_range` parameter: {list(view_range)}. "
                f"It should be within the range of lines of the file: [1, {len(lines)}]"
            )
        lines = lines[start - 1:end]

    body = clip(_numbered(lines, start=start), _MAX_VIEW)
    return f"Here's the content of {uri} with line numbers:\n{body}"


def _view_dir(path: Path, uri: str) -> str:
    """Listing, two levels deep, hidden items and node_modules excluded."""
    entries = [f"{_human_size(_dir_size(path))}\t{uri}"]
    for child in sorted(path.rglob("*")):
        rel = child.relative_to(path)
        if len(rel.parts) > 2:
            continue
        if any(p.startswith(".") or p == "node_modules" for p in rel.parts):
            continue
        size = _dir_size(child) if child.is_dir() else child.stat().st_size
        entries.append(f"{_human_size(size)}\t{_uri(child)}")
    header = (
        f"Here're the files and directories up to 2 levels deep in {uri}, "
        "excluding hidden items and node_modules:"
    )
    return header + "\n" + "\n".join(entries)


def _dir_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def _create(uri: str, file_text: str) -> str:
    path = _resolve(uri)
    if path == _root():
        return f"Error: {uri} is a directory"
    # Claude's tool description says create "creates or overwrites", so it will
    # legitimately call this on an existing path. Overwriting (rather than the
    # docs' alternative of erroring) keeps it from getting stuck re-reading a
    # file it means to replace.
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(file_text, encoding="utf-8")
    return f"File created successfully at: {uri}"


def _str_replace(uri: str, old: str, new: str) -> str:
    path = _resolve(uri)
    if not path.is_file():
        return f"Error: The path {uri} does not exist. Please provide a valid path."
    content = path.read_text(encoding="utf-8")

    count = content.count(old)
    if count == 0:
        return (
            f"No replacement was performed, old_str `{old}` did not appear "
            f"verbatim in {uri}."
        )
    if count > 1:
        hits = [
            str(i)
            for i, line in enumerate(content.splitlines(), start=1)
            if old in line
        ]
        return (
            f"No replacement was performed. Multiple occurrences of old_str "
            f"`{old}` in lines: {', '.join(hits)}. Please ensure it is unique"
        )

    updated = content.replace(old, new, 1)
    path.write_text(updated, encoding="utf-8")

    # A window around the edit, so Claude can confirm the result without a
    # second view call.
    line_no = content[: content.index(old)].count("\n") + 1
    lines = updated.splitlines()
    start = max(1, line_no - 3)
    end = min(len(lines), line_no + 3)
    snippet = _numbered(lines[start - 1:end], start=start)
    return f"The memory file has been edited.\n{snippet}"


def _insert(uri: str, insert_line: int, insert_text: str) -> str:
    path = _resolve(uri)
    if not path.is_file():
        return f"Error: The path {uri} does not exist"
    lines = path.read_text(encoding="utf-8").splitlines()
    if insert_line < 0 or insert_line > len(lines):
        return (
            f"Error: Invalid `insert_line` parameter: {insert_line}. It should "
            f"be within the range of lines of the file: [0, {len(lines)}]"
        )
    lines.insert(insert_line, insert_text.rstrip("\n"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return f"The file {uri} has been edited."


def _delete(uri: str) -> str:
    path = _resolve(uri)
    if path == _root():
        return f"Error: cannot delete the {_URI_ROOT} directory itself"
    if not path.exists():
        return f"Error: The path {uri} does not exist"
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()
    return f"Successfully deleted {uri}"


def _rename(old_uri: str, new_uri: str) -> str:
    source = _resolve(old_uri)
    dest = _resolve(new_uri)
    if source == _root():
        return f"Error: cannot rename the {_URI_ROOT} directory itself"
    if not source.exists():
        return f"Error: The path {old_uri} does not exist"
    if dest.exists():
        return f"Error: The destination {new_uri} already exists"
    dest.parent.mkdir(parents=True, exist_ok=True)
    source.rename(dest)
    return f"Successfully renamed {old_uri} to {new_uri}"
