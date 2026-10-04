"""Wayland input and capture for core/computer.py.

Wayland compositors ignore X11 synthetic input, so this drives the desktop
through the xdg-desktop-portal RemoteDesktop and ScreenCast interfaces. The
desktop may ask for approval when a session starts; while it lasts KDE shows a
"Remote Control" tray icon whose "End" entry stops it. If the user ends it, the
next action starts a new session.

`backend()` returns an object with the subset of the pyautogui API that
core/computer.py calls, so the computer tool runs on it unchanged.

One monitor is the "screen": the first approved stream, or the one picked by
CLAUDE_COMPUTER_MONITOR (an index, counting left to right). Screenshots come
from spectacle or grim and are cropped to that monitor.

Requires:  pip install dbus-next
"""

import asyncio
import contextlib
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

_PORTAL = "org.freedesktop.portal.Desktop"
_PORTAL_PATH = "/org/freedesktop/portal/desktop"
# dbus-next cannot parse the portal's full introspection (a property named
# "power-saver-enabled" is rejected), so only the calls used here are declared.
_INTROSPECTION = """<node>
 <interface name="org.freedesktop.portal.RemoteDesktop">
  <method name="CreateSession"><arg type="a{sv}" name="options" direction="in"/><arg type="o" name="handle" direction="out"/></method>
  <method name="SelectDevices"><arg type="o" name="session_handle" direction="in"/><arg type="a{sv}" name="options" direction="in"/><arg type="o" name="handle" direction="out"/></method>
  <method name="Start"><arg type="o" name="session_handle" direction="in"/><arg type="s" name="parent_window" direction="in"/><arg type="a{sv}" name="options" direction="in"/><arg type="o" name="handle" direction="out"/></method>
  <method name="NotifyPointerMotionAbsolute"><arg type="o" name="session_handle" direction="in"/><arg type="a{sv}" name="options" direction="in"/><arg type="u" name="stream" direction="in"/><arg type="d" name="x" direction="in"/><arg type="d" name="y" direction="in"/></method>
  <method name="NotifyPointerButton"><arg type="o" name="session_handle" direction="in"/><arg type="a{sv}" name="options" direction="in"/><arg type="i" name="button" direction="in"/><arg type="u" name="state" direction="in"/></method>
  <method name="NotifyPointerAxisDiscrete"><arg type="o" name="session_handle" direction="in"/><arg type="a{sv}" name="options" direction="in"/><arg type="u" name="axis" direction="in"/><arg type="i" name="steps" direction="in"/></method>
  <method name="NotifyKeyboardKeysym"><arg type="o" name="session_handle" direction="in"/><arg type="a{sv}" name="options" direction="in"/><arg type="i" name="keysym" direction="in"/><arg type="u" name="state" direction="in"/></method>
 </interface>
 <interface name="org.freedesktop.portal.ScreenCast">
  <method name="SelectSources"><arg type="o" name="session_handle" direction="in"/><arg type="a{sv}" name="options" direction="in"/><arg type="o" name="handle" direction="out"/></method>
 </interface>
</node>"""

_BUTTONS = {"left": 272, "right": 273, "middle": 274}  # linux input event codes
# One portal step moves Chrome about 12 px; a mouse-wheel notch, which is what a
# pyautogui scroll unit means, is about 120 px.
_STEPS_PER_NOTCH = 10
_APPROVAL_SECONDS = 150

# X11 keysyms for the key names core/computer.py hands over (pyautogui spelling).
_KEYSYMS = {
    "enter": 0xFF0D, "esc": 0xFF1B, "tab": 0xFF09, "backspace": 0xFF08, "delete": 0xFFFF,
    "home": 0xFF50, "left": 0xFF51, "up": 0xFF52, "right": 0xFF53, "down": 0xFF54,
    "pageup": 0xFF55, "pagedown": 0xFF56, "end": 0xFF57, "insert": 0xFF63,
    "space": 0x20, "shift": 0xFFE1, "ctrl": 0xFFE3, "capslock": 0xFFE5,
    "alt": 0xFFE9, "win": 0xFFEB, "printscreen": 0xFF61, "numlock": 0xFF7F,
    "menu": 0xFF67,
    **{f"f{n}": 0xFFBD + n for n in range(1, 13)},
}


def keysym(key: str) -> int:
    """X11 keysym for a pyautogui key name or a single character."""
    if key.lower() in _KEYSYMS:
        return _KEYSYMS[key.lower()]
    if len(key) == 1:
        return ord(key) if ord(key) < 0x100 else 0x01000000 + ord(key)
    raise ValueError(f"unknown key {key!r}")


class _Loop:
    """A private asyncio loop in a daemon thread that owns the D-Bus connection."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self.loop.run_forever, daemon=True, name="wayland-portal")
        self._thread.start()

    def run(self, coro, timeout: float):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(5)


class _Portal:
    """One RemoteDesktop + ScreenCast session."""

    def __init__(self) -> None:
        self.bus: Any = None
        self.rd: Any = None
        self.session: str = ""
        self.streams: list[dict] = []
        self._variant: Any = None
        self._futures: dict[str, asyncio.Future] = {}

    async def start(self) -> None:
        from dbus_next import BusType, Message, MessageType, Variant
        from dbus_next.aio import MessageBus

        self._variant = Variant
        loop = asyncio.get_running_loop()
        self.bus = await MessageBus(bus_type=BusType.SESSION).connect()
        sender = self.bus.unique_name[1:].replace(".", "_")

        def on_message(msg) -> bool:
            if (
                msg.message_type == MessageType.SIGNAL
                and msg.interface == "org.freedesktop.portal.Request"
                and msg.member == "Response"
            ):
                future = self._futures.get(msg.path)
                if future and not future.done():
                    future.set_result(msg.body)
            return False

        self.bus.add_message_handler(on_message)
        await self.bus.call(Message(
            destination="org.freedesktop.DBus", path="/org/freedesktop/DBus",
            interface="org.freedesktop.DBus", member="AddMatch", signature="s",
            body=["type='signal',interface='org.freedesktop.portal.Request',member='Response'"],
        ))
        obj = self.bus.get_proxy_object(_PORTAL, _PORTAL_PATH, _INTROSPECTION)
        self.rd = obj.get_interface("org.freedesktop.portal.RemoteDesktop")
        screencast = obj.get_interface("org.freedesktop.portal.ScreenCast")

        async def request(call, *args, options: dict, timeout: float = 30):
            token = "rm" + uuid.uuid4().hex[:12]
            options["handle_token"] = Variant("s", token)
            path = f"{_PORTAL_PATH}/request/{sender}/{token}"
            future = loop.create_future()
            self._futures[path] = future
            handle = await call(*args, options)
            if handle != path:
                self._futures[handle] = future
            code, results = await asyncio.wait_for(future, timeout)
            if code != 0:
                raise RuntimeError("remote control was not approved on the desktop")
            return results

        def plain(value):
            return value.value if isinstance(value, Variant) else value

        results = await request(self.rd.call_create_session, options={
            "session_handle_token": Variant("s", "rm" + uuid.uuid4().hex[:8]),
        })
        self.session = plain(results["session_handle"])
        await request(self.rd.call_select_devices, self.session, options={"types": Variant("u", 3)})
        await request(screencast.call_select_sources, self.session, options={
            "types": Variant("u", 1), "multiple": Variant("b", True), "cursor_mode": Variant("u", 1),
        })
        results = await request(self.rd.call_start, self.session, "", options={}, timeout=_APPROVAL_SECONDS)
        if plain(results.get("devices", 0)) & 3 != 3:
            raise RuntimeError("the desktop did not grant both pointer and keyboard control")
        for node, props in plain(results["streams"]):
            position, size = plain(props["position"]), plain(props["size"])
            self.streams.append({"node": node, "x": position[0], "y": position[1], "w": size[0], "h": size[1]})
        if not self.streams:
            raise RuntimeError("no monitor was shared with the session")

    async def pointer_to(self, node: int, x: float, y: float) -> None:
        await self.rd.call_notify_pointer_motion_absolute(self.session, {}, node, float(x), float(y))

    async def button(self, code: int, state: int) -> None:
        await self.rd.call_notify_pointer_button(self.session, {}, code, state)

    async def axis(self, axis: int, steps: int) -> None:
        await self.rd.call_notify_pointer_axis_discrete(self.session, {}, axis, steps)

    async def key(self, sym: int, state: int) -> None:
        await self.rd.call_notify_keyboard_keysym(self.session, {}, sym, state)

    async def close(self) -> None:
        from dbus_next import Message

        if self.bus is None:
            return
        try:
            if self.session:
                await self.bus.call(Message(
                    destination=_PORTAL, path=self.session,
                    interface="org.freedesktop.portal.Session", member="Close",
                ))
        finally:
            self.bus.disconnect()


def _capture_desktop():
    """PIL image of the whole desktop (every monitor)."""
    from PIL import Image

    commands = (["spectacle", "-b", "-n", "-f", "-o"], ["grim"])
    for command in commands:
        if not shutil.which(command[0]):
            continue
        with tempfile.TemporaryDirectory(prefix="rm-shot-") as folder:
            out = Path(folder) / "shot.png"
            subprocess.run([*command, str(out)], capture_output=True, timeout=30, check=False)
            if out.exists():
                image = Image.open(out)
                image.load()
                return image
    raise RuntimeError("no Wayland screenshot tool worked (install spectacle or grim)")


class PortalInput:
    """pyautogui-shaped input and capture for one monitor of a Wayland desktop."""

    FAILSAFE = False

    def __init__(self, loop, portal, stream: dict) -> None:
        self._loop = loop
        self._portal = portal
        self._stream = stream
        self._pos = (stream["w"] // 2, stream["h"] // 2)

    def _call(self, coro):
        try:
            return self._loop.run(coro, 30)
        except Exception as e:
            shutdown()
            raise RuntimeError(
                f"the remote control session is gone ({type(e).__name__}); "
                "the next action asks for approval again"
            ) from e

    def size(self) -> tuple[int, int]:
        return self._stream["w"], self._stream["h"]

    def position(self) -> tuple[int, int]:
        """Where this backend last moved the pointer; the portal cannot read it back."""
        return self._pos

    def moveTo(self, x: float, y: float, duration: float = 0.0) -> None:
        node = self._stream["node"]
        steps = max(1, round(duration / 0.02)) if duration else 1
        x0, y0 = self._pos
        for i in range(1, steps + 1):
            self._call(self._portal.pointer_to(node, x0 + (x - x0) * i / steps, y0 + (y - y0) * i / steps))
            if steps > 1:
                time.sleep(0.02)
        self._pos = (round(x), round(y))

    def click(self, button: str = "left", clicks: int = 1, interval: float = 0.0) -> None:
        for i in range(clicks):
            self.mouseDown(button)
            time.sleep(0.04)
            self.mouseUp(button)
            if i < clicks - 1:
                time.sleep(interval)

    def mouseDown(self, button: str = "left") -> None:
        self._call(self._portal.button(_BUTTONS[button], 1))

    def mouseUp(self, button: str = "left") -> None:
        self._call(self._portal.button(_BUTTONS[button], 0))

    def scroll(self, amount: int) -> None:
        """pyautogui counts positive as up; the portal counts positive as down."""
        self._call(self._portal.axis(0, -int(amount) * _STEPS_PER_NOTCH))

    def hscroll(self, amount: int) -> None:
        self._call(self._portal.axis(1, int(amount) * _STEPS_PER_NOTCH))

    def keyDown(self, key: str) -> None:
        self._call(self._portal.key(keysym(key), 1))

    def keyUp(self, key: str) -> None:
        self._call(self._portal.key(keysym(key), 0))

    def hotkey(self, *keys: str) -> None:
        for key in keys:
            self.keyDown(key)
        for key in reversed(keys):
            self.keyUp(key)

    def write(self, text: str, interval: float = 0.0) -> None:
        for char in text:
            name = {"\n": "enter", "\t": "tab"}.get(char, char)
            self.keyDown(name)
            self.keyUp(name)
            if interval:
                time.sleep(interval)

    def screenshot(self):
        image = _capture_desktop()
        right = max(s["x"] + s["w"] for s in self._portal.streams)
        scale = image.width / right if right else 1
        stream = self._stream
        return image.crop((
            round(stream["x"] * scale), round(stream["y"] * scale),
            round((stream["x"] + stream["w"]) * scale), round((stream["y"] + stream["h"]) * scale),
        ))


def available() -> str | None:
    """None when the portal route can be used, else the reason it cannot."""
    try:
        import dbus_next  # noqa: F401
    except ImportError:
        return "the dbus-next package is not installed (pip install dbus-next)"
    if not (shutil.which("spectacle") or shutil.which("grim")):
        return "no screenshot tool is installed (spectacle or grim)"
    return None


def _pick_stream(streams: list[dict]) -> dict:
    ordered = sorted(streams, key=lambda s: (s["x"], s["y"]))
    raw = os.getenv("CLAUDE_COMPUTER_MONITOR", "")
    if raw.isdigit() and int(raw) < len(ordered):
        return ordered[int(raw)]
    return ordered[0]


_state: dict[str, Any] = {"loop": None, "portal": None, "input": None}
_lock = threading.Lock()


def _connect() -> PortalInput:
    print(
        "[computer] Starting remote control of the desktop. If the desktop asks, approve it. "
        "While it lasts a 'Remote Control' tray icon shows; its 'End' entry stops it.",
        flush=True,
    )
    loop, portal = _Loop(), _Portal()
    try:
        loop.run(portal.start(), _APPROVAL_SECONDS + 15)
    except Exception:
        with contextlib.suppress(Exception):
            loop.run(portal.close(), 10)
        loop.stop()
        raise
    adapter = PortalInput(loop, portal, _pick_stream(portal.streams))
    _state.update(loop=loop, portal=portal, input=adapter)
    return adapter


def backend() -> PortalInput:
    """The shared session, created (and approved on the desktop) on first use."""
    with _lock:
        adapter = _state["input"]
        if adapter is None:
            adapter = _connect()
        return adapter


def shutdown() -> None:
    """End the session. Safe when none is open."""
    loop, portal = _state["loop"], _state["portal"]
    _state.update(loop=None, portal=None, input=None)
    if loop is None:
        return
    try:
        loop.run(portal.close(), 10)
    except Exception as e:
        print(f"[computer] closing the remote control session failed (ignored): {e}")
    loop.stop()
