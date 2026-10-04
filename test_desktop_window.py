"""Behavioural tests for core/desktop_window.py.

    python test_desktop_window.py

Argument handling, the generated KWin script and the output are tested with the
KWin call replaced. A last step lists the real windows (read-only) when a KDE
session is reachable, which exercises the D-Bus callback end to end.
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

FAILURES: list[str] = []
WINDOW = {
    "id": "{ad1a6e35-83e7-4e24-942a-5dbd500fef3c}", "title": "Dashboard | Claude Platform - Brave",
    "cls": "brave", "pid": 1, "output": "DP-2", "x": 0, "y": 32, "w": 1920, "h": 1048,
    "minimized": False, "fullscreen": False, "active": False,
}


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name}{' — ' + detail if detail else ''}")
        FAILURES.append(name)


def node_check(js: str) -> tuple[bool, str]:
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "w.js"
        path.write_text(js)
        result = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True, check=False)
    return result.returncode == 0, result.stderr[:200]


async def main_async(dw) -> None:
    print("arguments")
    check("list needs nothing", dw._validate({"action": "list"}) == ("list", {}, None))
    check("unknown action", dw._validate({"action": "close"})[2].startswith("Error: `action` must be one of"))
    check("activate needs a window", dw._validate({"action": "activate"})[2].startswith("Error: this action needs `window`"))
    check("blank window is refused", dw._validate({"action": "activate", "window": "  "})[2] is not None)
    check("move needs geometry", dw._validate({"action": "move", "window": "x"})[2].startswith("Error: `move` needs"))
    check("move rejects a non-integer", dw._validate({"action": "move", "window": "x", "x": "1"})[2] == "Error: `x` must be an integer")
    check("move rejects a bool", dw._validate({"action": "move", "window": "x", "x": True})[2] == "Error: `x` must be an integer")
    check("move rejects zero width", dw._validate({"action": "move", "window": "x", "width": 0})[2] == "Error: `width` must be at least 1")
    check("move keeps only the given fields",
          dw._validate({"action": "move", "window": " brave ", "x": 5, "height": 700})[1] == {"window": "brave", "x": 5, "height": 700})
    check("fullscreen enabled must be a bool", dw._validate({"action": "fullscreen", "window": "x", "enabled": "no"})[2] == "Error: `enabled` must be true or false")

    print("the KWin script")
    hostile = '"); callDBus("evil"); ("'
    js = dw._script("abc123", "org.researchmesh.router.p1", "move", {"window": hostile, "x": 5})
    check("arguments are JSON literals", f"ARGS = {json.dumps({'window': hostile, 'x': 5})};" in js)
    check("no token is left unreplaced", "@REQ@" not in js and "@ARGS@" not in js and "@ACTION@" not in js and "@SERVICE@" not in js)
    if shutil.which("node"):
        ok, detail = await asyncio.to_thread(node_check, js)
        check("the script is valid JavaScript", ok, detail)
    else:
        print("  skipped: node is not installed")

    print("output")
    listing = {"ok": True, "screens": [{"name": "DP-2", "x": 0, "y": 0, "w": 1920, "h": 1080}], "windows": [{**WINDOW, "active": True}]}
    text = dw._render("list", listing)
    check("list shows screens and windows", text.startswith("Screens: DP-2 0,0 1920x1080\nWindows (1):") and "ad1a6e35" in text and "active" in text, text)
    check("a long title is cut", "..." in dw._row({**WINDOW, "title": "t" * 120}))
    text = dw._render("activate", {"ok": False, "error": "more than one window matches", "matches": [WINDOW, {**WINDOW, "id": "{ffff0000-0000-0000-0000-000000000000}"}]})
    check("an ambiguous match lists the candidates", text.startswith("Error: more than one window matches\n") and text.count("\n") == 2, text)
    check("no match", dw._render("activate", {"ok": False, "error": "no window matches"}) == "Error: no window matches")

    print("actions settle before they report")
    calls: list[tuple] = []

    def fake_run(action, args):
        calls.append((action, args))
        if action == "list":
            return {"ok": True, "screens": [], "windows": [{**WINDOW, "active": True}]}
        return {"ok": True, "window": dict(WINDOW)}

    saved_run, saved_available, saved_sleep = dw._run_script, dw.available, dw.time.sleep
    dw._run_script, dw.available, dw.time.sleep = fake_run, (lambda: None), (lambda s: None)
    try:
        out = await dw.execute("desktop_window", {"action": "activate", "window": "brave"})
        check("the action then a read-back are sent", [c[0] for c in calls] == ["activate", "list"], str(calls))
        check("the settled state is reported", out.startswith("activate done:") and "active" in out, out)
        calls.clear()
        await dw.execute("desktop_window", {"action": "list"})
        check("list is a single call", [c[0] for c in calls] == ["list"])

        def boom(action, args):
            raise ConnectionError("no bus")

        dw._run_script = boom
        out = await dw.execute("desktop_window", {"action": "list"})
        check("a failure names KDE", out.startswith("Error: desktop_window failed (ConnectionError: no bus)") and "KDE Plasma" in out, out)
        dw.available = lambda: "the dbus-next package is not installed (pip install dbus-next)"
        check("a missing package is named", (await dw.execute("desktop_window", {"action": "list"})).startswith("Error: desktop_window cannot run: the dbus-next"))
    finally:
        dw._run_script, dw.available, dw.time.sleep = saved_run, saved_available, saved_sleep

    print("real KWin, read-only")
    try:
        result = await asyncio.to_thread(dw._call, "list", {})
    except Exception as e:
        print(f"  skipped: no reachable KWin ({type(e).__name__})")
    else:
        check("windows and screens come back over the callback", result.get("ok") is True and result["screens"] and isinstance(result["windows"], list), str(result)[:200])
        check("window records have the fields the tool prints", all({"id", "title", "cls", "output", "x", "y", "w", "h"} <= set(w) for w in result["windows"]))
        text = await dw.execute("desktop_window", {"action": "list"})
        check("the rendered list is non-empty", text.startswith("Screens: ") and "Windows (" in text, text[:120])
        out = await dw.execute("desktop_window", {"action": "activate", "window": "zzz-no-such-window-zzz"})
        check("an unknown window is an error and changes nothing", out == "Error: no window matches", out)
    finally:
        dw.shutdown()


def main() -> int:
    from core import desktop_window

    asyncio.run(main_async(desktop_window))
    total = len(FAILURES)
    print(f"\n{'FAILED' if total else 'all checks passed'}" + (f": {total} failure(s)" if total else ""))
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
