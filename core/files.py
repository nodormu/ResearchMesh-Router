"""Recoverable deletes — send2trash.

There is no approval gate in this client: whatever the model decides to run,
runs. `rm` through the bash tool is therefore one bad path expansion away from
being unrecoverable, while the XDG trash is a cheap undo. This tool exists so
the model has a delete that is not final.

Requires:  pip install send2trash
"""

import asyncio
import json
from pathlib import Path

TOOLS = [
    {
        "name": "trash",
        "description": (
            "Delete files or directories to the desktop trash, where they can be "
            "restored. Use this instead of 'rm' in the bash tool for ANY deletion "
            "the user did not explicitly ask to be permanent — it is the only "
            "recoverable delete available here."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Files or directories to move to the trash.",
                }
            },
            "required": ["paths"],
        },
    }
]

_TOOL_NAMES = {t["name"] for t in TOOLS}


def handles(name: str) -> bool:
    return name in _TOOL_NAMES


async def execute(name: str, tool_input: dict) -> str:
    if name != "trash":
        return json.dumps({"error": f"unknown file tool {name!r}"})
    return await asyncio.to_thread(_run, tool_input)


def _run(tool_input: dict) -> str:
    raw = tool_input.get("paths") or []
    if isinstance(raw, str):  # tolerate a single path sent as a bare string
        raw = [raw]
    if not raw:
        return json.dumps({"error": "no paths provided"})

    try:
        from send2trash import send2trash
        from send2trash.exceptions import TrashPermissionError
    except ImportError:
        return json.dumps(
            {
                "error": "send2trash is not installed — `pip install send2trash` "
                "to enable the trash tool"
            }
        )

    results = []
    for item in raw:
        # send2trash needs an absolute path; a relative one fails with a bare
        # "Permission denied: ''" that says nothing about the real cause.
        path = Path(str(item)).expanduser().absolute()
        if not path.exists():
            results.append({"path": str(path), "trashed": False, "error": "no such path"})
            continue
        try:
            send2trash(str(path))
            results.append({"path": str(path), "trashed": True})
        except TrashPermissionError:
            # GIO refuses to trash from tmpfs and other "system internal"
            # mounts, and raises this with an empty message — say why instead.
            results.append(
                {
                    "path": str(path),
                    "trashed": False,
                    "error": (
                        "no trash is available for this location (tmpfs mounts "
                        "such as /tmp cannot be trashed to). Deleting it would "
                        "have to be permanent — ask the user first."
                    ),
                }
            )
        except Exception as e:
            results.append({"path": str(path), "trashed": False, "error": str(e)})

    return json.dumps(
        {
            "results": results,
            "trashed": sum(1 for r in results if r["trashed"]),
            "failed": sum(1 for r in results if not r["trashed"]),
        }
    )
