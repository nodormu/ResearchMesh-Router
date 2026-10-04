"""`desktop_window` — list, focus, move and resize windows on a KDE desktop.

KWin is scripted over D-Bus: a one-shot script runs inside the compositor and
calls back into this process (`callDBus`) with a JSON result. This process
exports a small service on the session bus for that callback. It works on the
Wayland and X11 sessions of KDE Plasma.

Typing goes to whichever window has focus, and a click through the `computer`
tool does not always move focus, so this is how the agent puts the right window
in front before it types.

Requires:  pip install dbus-next
"""

import asyncio
import json
import os
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from core.dbus_loop import Loop
from core.output import clip

TOOLS = [
    {
        "name": "desktop_window",
        "description": (
            "List the desktop's windows, or bring one to the front, move or resize "
            "it, make it full screen, or minimize it. KDE Plasma only. Use it before "
            "`computer` when keystrokes must reach a particular window: typing goes "
            "to whichever window has focus. `window` is a window id from `list`, or "
            "part of its title or application class (for example `brave` or "
            "`Dolphin`); if more than one window matches, the matches are listed and "
            "nothing changes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "activate", "move", "fullscreen", "minimize", "restore"],
                },
                "window": {"type": "string", "description": "Id, title or class, for every action but `list`."},
                "x": {"type": "integer", "description": "Left edge in desktop pixels, for `move`."},
                "y": {"type": "integer", "description": "Top edge in desktop pixels, for `move`."},
                "width": {"type": "integer", "description": "Width in pixels, for `move`."},
                "height": {"type": "integer", "description": "Height in pixels, for `move`."},
                "enabled": {
                    "type": "boolean",
                    "description": "For `fullscreen`: false leaves full screen. Default true.",
                },
            },
            "required": ["action"],
        },
    }
]

_NAMES = {"desktop_window"}
_ACTIONS = ("list", "activate", "move", "fullscreen", "minimize", "restore")
_SERVICE_PATH = "/windows"
_INTERFACE = "org.researchmesh.Windows"
_MAX_TEXT = 6000
_REPLY_SECONDS = 8

_SCRIPTING_XML = """<node>
 <interface name="org.kde.kwin.Scripting">
  <method name="loadScript"><arg type="s" name="path" direction="in"/><arg type="s" name="name" direction="in"/><arg type="i" direction="out"/></method>
  <method name="unloadScript"><arg type="s" name="name" direction="in"/><arg type="b" direction="out"/></method>
 </interface>
</node>"""
_SCRIPT_XML = """<node>
 <interface name="org.kde.kwin.Script">
  <method name="run"/>
 </interface>
</node>"""

# Runs inside KWin. @NAME@ tokens are replaced with JSON literals, so nothing the
# model supplies is ever interpreted as code.
_SCRIPT = """(function () {
  var REQ = @REQ@, SERVICE = @SERVICE@, ACTION = @ACTION@, ARGS = @ARGS@;
  function all() {
    return (typeof workspace.windowList === "function") ? workspace.windowList() : workspace.clientList();
  }
  function usable(w) {
    return w.caption !== "" && !w.desktopWindow && !w.dock && !w.specialWindow && (w.normalWindow || w.dialog);
  }
  function info(w) {
    var g = w.frameGeometry;
    return {id: String(w.internalId), title: w.caption, cls: String(w.resourceClass), pid: w.pid,
            output: w.output ? w.output.name : "", x: Math.round(g.x), y: Math.round(g.y),
            w: Math.round(g.width), h: Math.round(g.height), minimized: !!w.minimized,
            fullscreen: !!w.fullScreen, active: workspace.activeWindow === w};
  }
  function pick(term) {
    var t = String(term).toLowerCase(), usableWindows = all().filter(usable);
    var byId = usableWindows.filter(function (w) { return String(w.internalId).toLowerCase().indexOf(t) >= 0; });
    if (byId.length) { return byId; }
    return usableWindows.filter(function (w) {
      return w.caption.toLowerCase().indexOf(t) >= 0 || String(w.resourceClass).toLowerCase().indexOf(t) >= 0;
    });
  }
  var result = {ok: true};
  if (ACTION === "list") {
    result.windows = all().filter(usable).map(info);
    result.screens = workspace.screens.map(function (s) {
      return {name: s.name, x: s.geometry.x, y: s.geometry.y, w: s.geometry.width, h: s.geometry.height};
    });
  } else {
    var found = pick(ARGS.window);
    if (found.length === 0) {
      result = {ok: false, error: "no window matches"};
    } else if (found.length > 1) {
      result = {ok: false, error: "more than one window matches", matches: found.map(info)};
    } else {
      var w = found[0];
      if (ACTION === "activate") { w.minimized = false; workspace.activeWindow = w; }
      else if (ACTION === "restore") { w.minimized = false; w.fullScreen = false; }
      else if (ACTION === "minimize") { w.minimized = true; }
      else if (ACTION === "fullscreen") { w.fullScreen = ARGS.enabled !== false; }
      else if (ACTION === "move") {
        var g = w.frameGeometry;
        w.frameGeometry = {x: ARGS.x !== undefined ? ARGS.x : g.x, y: ARGS.y !== undefined ? ARGS.y : g.y,
                           width: ARGS.width !== undefined ? ARGS.width : g.width,
                           height: ARGS.height !== undefined ? ARGS.height : g.height};
      }
      result.window = info(w);
    }
  }
  callDBus(SERVICE, "/windows", "org.researchmesh.Windows", "Report", REQ, JSON.stringify(result));
})();
"""

_state: dict[str, Any] = {"loop": None, "bus": None, "name": "", "pending": {}}
_lock = threading.Lock()


def handles(name: str) -> bool:
    return name in _NAMES


def _validate(tool_input: dict) -> tuple[str, dict, str | None]:
    """(action, script arguments, error)."""
    action = tool_input.get("action")
    if action not in _ACTIONS:
        return "", {}, f"Error: `action` must be one of {', '.join(_ACTIONS)}"
    args: dict[str, Any] = {}
    if action != "list":
        window = tool_input.get("window")
        if not isinstance(window, str) or not window.strip():
            return "", {}, "Error: this action needs `window` (an id, or part of a title or class)"
        args["window"] = window.strip()
    if action == "move":
        for key in ("x", "y", "width", "height"):
            if key in tool_input:
                value = tool_input[key]
                if isinstance(value, bool) or not isinstance(value, int):
                    return "", {}, f"Error: `{key}` must be an integer"
                if key in ("width", "height") and value < 1:
                    return "", {}, f"Error: `{key}` must be at least 1"
                args[key] = value
        if not any(key in args for key in ("x", "y", "width", "height")):
            return "", {}, "Error: `move` needs at least one of x, y, width, height"
    if action == "fullscreen" and "enabled" in tool_input:
        if not isinstance(tool_input["enabled"], bool):
            return "", {}, "Error: `enabled` must be true or false"
        args["enabled"] = tool_input["enabled"]
    return str(action), args, None


def _script(request: str, service: str, action: str, args: dict) -> str:
    out = _SCRIPT
    for token, value in (("@REQ@", request), ("@SERVICE@", service), ("@ACTION@", action), ("@ARGS@", args)):
        out = out.replace(token, json.dumps(value))
    return out


def _row(window: dict) -> str:
    flags = [name for name in ("active", "minimized", "fullscreen") if window.get(name)]
    short = window["id"].strip("{}")[:8]
    title = window["title"] if len(window["title"]) <= 80 else window["title"][:77] + "..."
    return (
        f'{short}  {window["output"] or "-":<6} {window["x"]},{window["y"]} {window["w"]}x{window["h"]}'
        f'  {",".join(flags) or "-":<10} {window["cls"]}  "{title}"'
    )


def _render(action: str, result: dict) -> str:
    if not result.get("ok"):
        text = f"Error: {result.get('error', 'desktop_window failed')}"
        if result.get("matches"):
            text += "\n" + "\n".join(_row(w) for w in result["matches"])
        return text
    if action == "list":
        screens = "; ".join(f'{s["name"]} {s["x"]},{s["y"]} {s["w"]}x{s["h"]}' for s in result["screens"])
        rows = [_row(w) for w in result["windows"][:40]]
        return clip(f"Screens: {screens}\nWindows ({len(result['windows'])}):\n" + "\n".join(rows), _MAX_TEXT)
    return f"{action} done:\n{_row(result['window'])}"


async def _connect() -> None:
    from dbus_next import BusType
    from dbus_next.aio import MessageBus
    from dbus_next.service import ServiceInterface, method

    class Reports(ServiceInterface):
        def __init__(self, pending: dict) -> None:
            super().__init__(_INTERFACE)
            self._pending = pending

        @method()
        def Report(self, request: "s", payload: "s"):  # type: ignore[name-defined]  # noqa: F821
            future = self._pending.get(request)
            if future is not None and not future.done():
                future.set_result(payload)

    bus = await MessageBus(bus_type=BusType.SESSION).connect()
    bus.export(_SERVICE_PATH, Reports(_state["pending"]))
    name = f"org.researchmesh.router.p{os.getpid()}"
    await bus.request_name(name)
    _state.update(bus=bus, name=name)


async def _exchange(action: str, args: dict) -> dict:
    """Load a KWin script, run it, and wait for its callback."""
    bus, service = _state["bus"], _state["name"]
    request = uuid.uuid4().hex
    future = asyncio.get_running_loop().create_future()
    _state["pending"][request] = future
    folder = Path(tempfile.mkdtemp(prefix="rm-kwin-"))
    plugin = f"rm-window-{request[:12]}"
    scripting = None
    try:
        path = folder / "window.js"
        path.write_text(_script(request, service, action, args))
        root = bus.get_proxy_object("org.kde.KWin", "/Scripting", _SCRIPTING_XML)
        scripting = root.get_interface("org.kde.kwin.Scripting")
        script_id = await scripting.call_load_script(str(path), plugin)
        if script_id < 0:
            raise RuntimeError("KWin refused to load the script")
        script = bus.get_proxy_object("org.kde.KWin", f"/Scripting/Script{script_id}", _SCRIPT_XML)
        await script.get_interface("org.kde.kwin.Script").call_run()
        payload = await asyncio.wait_for(future, _REPLY_SECONDS)
        return json.loads(payload)
    finally:
        _state["pending"].pop(request, None)
        if scripting is not None:
            try:
                await scripting.call_unload_script(plugin)
            except Exception as e:
                print(f"[desktop_window] unloading the KWin script failed (ignored): {e}")
        shutil.rmtree(folder, ignore_errors=True)


def available() -> str | None:
    """None when this can run, else the reason it cannot."""
    try:
        import dbus_next  # noqa: F401
    except ImportError:
        return "the dbus-next package is not installed (pip install dbus-next)"
    return None


def _run_script(action: str, args: dict) -> dict:
    with _lock:
        if _state["loop"] is None:
            loop = Loop("desktop-window")
            try:
                loop.run(_connect(), 15)
            except Exception:
                loop.stop()
                raise
            _state["loop"] = loop
        loop = _state["loop"]
    return loop.run(_exchange(action, args), _REPLY_SECONDS + 10)


def _call(action: str, args: dict) -> dict:
    result = _run_script(action, args)
    if action != "list" and result.get("ok"):
        # Focus and geometry change asynchronously; read the settled state back.
        time.sleep(0.6)
        listing = _run_script("list", {})
        wid = result["window"]["id"]
        now = next((w for w in listing.get("windows", []) if w["id"] == wid), None)
        if now:
            result["window"] = now
    return result


async def execute(name: str, tool_input: dict) -> str:
    if name not in _NAMES:
        return f"Error: unknown tool {name!r}"
    action, args, error = _validate(tool_input)
    if error:
        return error
    reason = available()
    if reason:
        return f"Error: desktop_window cannot run: {reason}"
    try:
        result = await asyncio.to_thread(_call, action, args)
    except Exception as e:
        return (
            f"Error: desktop_window failed ({type(e).__name__}: {e}). It needs KDE Plasma "
            "(KWin must be on the session bus)."
        )
    return _render(action, result)


def shutdown() -> None:
    """Close the D-Bus connection. Safe when none is open."""
    loop, bus = _state["loop"], _state["bus"]
    _state.update(loop=None, bus=None, name="")
    if loop is None:
        return
    if bus is not None:
        try:
            bus.disconnect()
        except Exception as e:
            print(f"[desktop_window] closing the D-Bus connection failed (ignored): {e}")
    loop.stop()
