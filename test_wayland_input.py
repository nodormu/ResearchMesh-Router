"""Behavioural tests for core/wayland_input.py and the computer tool running on it.

    python test_wayland_input.py

No desktop is touched: a fake portal records every call the adapter makes. The
real portal session needs a desktop approval dialog and is exercised by hand.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

FAILURES: list[str] = []
STREAMS = [
    {"node": 80, "x": 1920, "y": 0, "w": 1920, "h": 1080},
    {"node": 82, "x": 0, "y": 0, "w": 1920, "h": 1080},
]


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name}{' — ' + detail if detail else ''}")
        FAILURES.append(name)


class FakeLoop:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.stopped = False

    def run(self, coro, timeout):
        if self.fail:
            coro.close()
            raise ConnectionError("bus gone")
        return asyncio.run(coro)

    def stop(self) -> None:
        self.stopped = True


class FakePortal:
    def __init__(self) -> None:
        self.streams = STREAMS
        self.events: list[tuple] = []

    async def pointer_to(self, node, x, y):
        self.events.append(("move", node, round(x, 1), round(y, 1)))

    async def button(self, code, state):
        self.events.append(("button", code, state))

    async def axis(self, axis, steps):
        self.events.append(("axis", axis, steps))

    async def key(self, sym, state):
        self.events.append(("key", sym, state))

    async def close(self):
        self.events.append(("close",))


def make(wi):
    portal = FakePortal()
    adapter = wi.PortalInput(FakeLoop(), portal, wi._pick_stream(STREAMS))
    return adapter, portal


def desktop_image(scale: int = 1):
    from PIL import Image

    image = Image.new("RGB", (3840 * scale, 1080 * scale), (255, 0, 0))
    image.paste((0, 0, 255), (1920 * scale, 0, 3840 * scale, 1080 * scale))
    return image


def main_checks(wi, computer) -> None:
    print("key names map to X11 keysyms")
    check("letter", wi.keysym("a") == 0x61 and wi.keysym("A") == 0x41)
    check("named keys", wi.keysym("enter") == 0xFF0D and wi.keysym("ctrl") == 0xFFE3 and wi.keysym("esc") == 0xFF1B)
    check("function keys", wi.keysym("f1") == 0xFFBE and wi.keysym("f5") == 0xFFC2 and wi.keysym("f12") == 0xFFC9)
    check("latin-1 and beyond", wi.keysym("é") == 0xE9 and wi.keysym("€") == 0x01000000 + 0x20AC)
    try:
        wi.keysym("nonsense")
        check("unknown key is an error", False)
    except ValueError:
        check("unknown key is an error", True)

    print("the screen is one monitor")
    os.environ.pop("CLAUDE_COMPUTER_MONITOR", None)
    check("default is the leftmost monitor", wi._pick_stream(STREAMS)["node"] == 82)
    os.environ["CLAUDE_COMPUTER_MONITOR"] = "1"
    check("CLAUDE_COMPUTER_MONITOR=1 picks the next one", wi._pick_stream(STREAMS)["node"] == 80)
    os.environ["CLAUDE_COMPUTER_MONITOR"] = "9"
    check("an index out of range falls back", wi._pick_stream(STREAMS)["node"] == 82)
    os.environ.pop("CLAUDE_COMPUTER_MONITOR")

    print("pointer")
    adapter, portal = make(wi)
    check("size is the monitor size", adapter.size() == (1920, 1080))
    adapter.moveTo(700, 500)
    check("moveTo sends monitor-relative coordinates", portal.events == [("move", 82, 700.0, 500.0)], str(portal.events))
    check("position is tracked", adapter.position() == (700, 500))
    portal.events.clear()
    adapter.moveTo(900, 700, duration=0.1)
    moves = [e for e in portal.events if e[0] == "move"]
    check("a timed move is interpolated and lands on target", len(moves) > 1 and moves[-1][2:] == (900.0, 700.0), str(moves[-3:]))
    portal.events.clear()
    adapter.click()
    check("left click is press then release", portal.events == [("button", 272, 1), ("button", 272, 0)], str(portal.events))
    portal.events.clear()
    adapter.click(button="right")
    check("right click uses the right button", portal.events == [("button", 273, 1), ("button", 273, 0)])
    portal.events.clear()
    adapter.click(clicks=2, interval=0.01)
    check("double click is two presses", portal.events == [("button", 272, 1), ("button", 272, 0)] * 2)
    portal.events.clear()
    adapter.mouseDown()
    adapter.mouseUp()
    check("mouseDown and mouseUp", portal.events == [("button", 272, 1), ("button", 272, 0)])

    print("scroll")
    portal.events.clear()
    adapter.scroll(3)
    adapter.scroll(-2)
    adapter.hscroll(4)
    check("positive pyautogui scroll is up, which is negative for the portal; a notch is 10 steps",
          portal.events == [("axis", 0, -30), ("axis", 0, 20), ("axis", 1, 40)], str(portal.events))

    print("keys")
    portal.events.clear()
    adapter.hotkey("ctrl", "a")
    check("a chord presses in order and releases in reverse", portal.events == [
        ("key", 0xFFE3, 1), ("key", 0x61, 1), ("key", 0x61, 0), ("key", 0xFFE3, 0)], str(portal.events))
    portal.events.clear()
    adapter.write("Ab\n")
    check("typing taps each character, newline is Enter", portal.events == [
        ("key", 0x41, 1), ("key", 0x41, 0), ("key", 0x62, 1), ("key", 0x62, 0),
        ("key", 0xFF0D, 1), ("key", 0xFF0D, 0)], str(portal.events))

    print("screenshot is cropped to the chosen monitor")
    saved = wi._capture_desktop
    try:
        wi._capture_desktop = lambda: desktop_image()
        left, _ = make(wi)
        shot = left.screenshot()
        check("left monitor: size and colour", shot.size == (1920, 1080) and shot.getpixel((10, 10)) == (255, 0, 0))
        right = wi.PortalInput(FakeLoop(), FakePortal(), STREAMS[0])
        shot = right.screenshot()
        check("right monitor: size and colour", shot.size == (1920, 1080) and shot.getpixel((10, 10)) == (0, 0, 255))
        wi._capture_desktop = lambda: desktop_image(scale=2)
        shot = right.screenshot()
        check("a scaled capture is cropped in its own pixels", shot.size == (3840, 2160) and shot.getpixel((10, 10)) == (0, 0, 255))
    finally:
        wi._capture_desktop = saved

    print("a lost session is reported and recovers on the next action")
    dead = wi.PortalInput(FakeLoop(fail=True), FakePortal(), STREAMS[1])
    wi._state.update(loop=FakeLoop(), portal=FakePortal(), input=dead)
    try:
        dead.click()
        check("error raised", False)
    except RuntimeError as e:
        check("error says the session is gone and approval returns", "session is gone" in str(e) and "asks for approval again" in str(e), str(e))
    check("shared session was cleared", wi._state["input"] is None and wi._state["loop"] is None)

    print("the computer tool runs on the backend")
    adapter, portal = make(wi)
    saved_capture, saved_backend = wi._capture_desktop, wi.backend
    saved_guard, saved_wayland = computer._guard, computer._wayland_session
    wi._capture_desktop = lambda: desktop_image()
    wi.backend = lambda: adapter
    computer._guard = lambda: None
    computer._wayland_session = lambda: True
    try:
        out = computer._run("left_click", {"coordinate": [640, 400]})
        check("a click at the declared centre lands at the monitor centre",
              portal.events[0] == ("move", 82, 960.0, 540.0) and ("button", 272, 1) in portal.events, str(portal.events))
        check("the result carries the follow-up screenshot", isinstance(out, dict), repr(out)[:100])
        out = computer._run("cursor_position", {})
        check("cursor_position reports the tracked position in declared space", out == "X=640, Y=400", out if isinstance(out, str) else "")
        portal.events.clear()
        computer._run("scroll", {"scroll_direction": "down", "scroll_amount": 5})
        check("scroll down is positive for the portal", ("axis", 0, 50) in portal.events, str(portal.events))
        portal.events.clear()
        computer._run("key", {"text": "ctrl+shift+t"})
        check("key chord names are mapped", [e for e in portal.events if e[0] == "key"][:3] ==
              [("key", 0xFFE3, 1), ("key", 0xFFE1, 1), ("key", 0x74, 1)], str(portal.events))

        def refuse():
            raise RuntimeError("remote control was not approved on the desktop")

        wi.backend = refuse
        out = computer._run("left_click", {"coordinate": [1, 1]})
        check("an unapproved session is reported",
              out == "Error: remote control of the Wayland desktop failed: remote control was not approved on the desktop", str(out))
    finally:
        wi._capture_desktop, wi.backend = saved_capture, saved_backend
        computer._guard, computer._wayland_session = saved_guard, saved_wayland

    print("session choice and guard")
    saved_env = {k: os.environ.get(k) for k in ("CLAUDE_COMPUTER_FORCE", "XDG_SESSION_TYPE", "WAYLAND_DISPLAY", "DISPLAY")}
    saved_available = wi.available
    try:
        os.environ.pop("CLAUDE_COMPUTER_FORCE", None)
        os.environ["XDG_SESSION_TYPE"] = "wayland"
        check("a Wayland session uses the portal", computer._wayland_session() is True)
        os.environ["CLAUDE_COMPUTER_FORCE"] = "1"
        check("CLAUDE_COMPUTER_FORCE=1 puts it back on X11", computer._wayland_session() is False and computer._guard() is None)
        os.environ.pop("CLAUDE_COMPUTER_FORCE")
        wi.available = lambda: "the dbus-next package is not installed (pip install dbus-next)"
        message = computer._guard() or ""
        check("a missing prerequisite is named", "portal route is unavailable: the dbus-next package" in message, message[:120])
        wi.available = lambda: None
        check("with the prerequisites the guard passes", computer._guard() is None)
        os.environ["XDG_SESSION_TYPE"] = "x11"
        os.environ.pop("WAYLAND_DISPLAY", None)
        os.environ.pop("DISPLAY", None)
        check("X11 with no DISPLAY is still refused", "no DISPLAY" in (computer._guard() or ""))
    finally:
        wi.available = saved_available
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def main() -> int:
    from core import computer
    from core import wayland_input as wi

    main_checks(wi, computer)
    total = len(FAILURES)
    print(f"\n{'FAILED' if total else 'all checks passed'}" + (f": {total} failure(s)" if total else ""))
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
