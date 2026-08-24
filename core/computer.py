"""`computer` — Anthropic's client-executed computer use tool (`computer_20251124`).

A learned schema: Claude already knows the action vocabulary (screenshot, clicks,
type, key, scroll, drag, zoom, …), so there is no description to write. This
module supplies the eyes and hands — screen capture via Pillow/pyautogui, input
via pyautogui — and, more importantly, the coordinate contract.

**Coordinates.** Claude returns coordinates in the space of the image it was
sent, so the declared `display_width_px`/`display_height_px` must match the
screenshot's real pixel dimensions or every click lands offset. Real screens are
usually larger than the ~1.15MP that reads well, so this module declares one
fixed logical size (`CLAUDE_DISPLAY_SIZE`, default 1280x800), always downscales
captures to exactly that, and scales Claude's coordinates back up to native
screen space. Declared size and sent image can therefore never drift apart.

**This tool is beta-gated.** `computer_20251124` requires the
`computer-use-2025-11-24` beta header, which is why core/claude.py posts to
`client.beta.messages.create` — see BETAS there. Declaring this tool without
that header is a 400 on every request, not just computer-use ones.

**Wayland.** Input synthesis and screen capture here go through X11/XTEST. On a
Wayland session that reaches XWayland clients at best and native Wayland windows
not at all, so rather than silently clicking into the void the tool refuses and
says why. Override with CLAUDE_COMPUTER_FORCE=1 (useful under XWayland-only
setups); the real fix is an Xorg session or a nested X server such as Xvfb.
"""

import asyncio
import base64
import io
import os
import subprocess
import time

from core.output import image_result

# Anthropic's benchmarked baselines are 1280x800 for web apps and 1024x768 /
# 1280x720 for general desktop work; below ~1280x720 accuracy drops off.
_DEFAULT_SIZE = (1280, 800)


def _declared_size() -> tuple[int, int]:
    raw = os.getenv("CLAUDE_DISPLAY_SIZE", "")
    if raw:
        try:
            w, h = (int(part) for part in raw.lower().split("x", 1))
            if w > 0 and h > 0:
                return w, h
        except ValueError:
            pass
    return _DEFAULT_SIZE


DISPLAY_WIDTH, DISPLAY_HEIGHT = _declared_size()

COMPUTER_TOOL = {
    "type": "computer_20251124",
    "name": "computer",
    "display_width_px": DISPLAY_WIDTH,
    "display_height_px": DISPLAY_HEIGHT,
    # Lets Claude re-inspect a region at native resolution instead of asking for
    # a second full screenshot — cheaper, and the only way to read small text
    # once the capture has been downscaled to the declared size.
    "enable_zoom": True,
}
TOOLS = [COMPUTER_TOOL]

# The beta header this tool requires. core/claude.py reads it from here so the
# header and the tool version can never drift apart.
BETA_FLAG = "computer-use-2025-11-24"

# Let the UI repaint before we capture the result of an action.
_SETTLE = 0.4

# Claude uses xdotool-style key names; pyautogui has its own spelling.
_KEY_ALIASES = {
    "return": "enter",
    "kp_enter": "enter",
    "escape": "esc",
    "backspace": "backspace",
    "page_up": "pageup",
    "prior": "pageup",
    "page_down": "pagedown",
    "next": "pagedown",
    "super": "win",
    "super_l": "win",
    "meta": "win",
    "control": "ctrl",
    "control_l": "ctrl",
    "alt_l": "alt",
    "shift_l": "shift",
}

# Actions that never get a follow-up screenshot appended: `wait` changes nothing
# worth re-capturing, and screenshot/zoom already *are* the capture — appending
# to them would double the image on success and double the error on failure.
_NO_SCREENSHOT = {"wait", "screenshot", "zoom"}


def handles(name: str) -> bool:
    return name == "computer"


async def execute(name: str, tool_input: dict) -> str | dict:
    if name != "computer":
        return f"Error: {name} is not handled by the computer tool"
    return await asyncio.to_thread(_run, tool_input)


def _guard() -> str | None:
    """Refuse up front on a session where input synthesis silently no-ops."""
    if os.getenv("CLAUDE_COMPUTER_FORCE") == "1":
        return None
    if os.getenv("XDG_SESSION_TYPE", "").lower() == "wayland" or os.getenv(
        "WAYLAND_DISPLAY"
    ):
        return (
            "Error: this is a Wayland session. The computer tool drives the "
            "screen through X11/XTEST, which Wayland compositors ignore for "
            "security — clicks and keystrokes would not reach native Wayland "
            "windows, and screenshots would come back blank or partial. Log in "
            "to an Xorg session, or run this client under a nested X server "
            "(e.g. `xvfb-run -s '-screen 0 1280x800x24' python main.py`). To "
            "attempt it anyway on an XWayland-only setup, set "
            "CLAUDE_COMPUTER_FORCE=1."
        )
    if not os.getenv("DISPLAY"):
        return (
            "Error: no DISPLAY is set, so there is no screen to control. Start "
            "an X server or run under xvfb-run."
        )
    return None


def _run(tool_input: dict) -> str | dict:
    blocked = _guard()
    if blocked:
        return blocked

    action = tool_input.get("action")
    if not action:
        return "Error: no action provided"

    try:
        import pyautogui
    except Exception as e:  # ImportError, or X11 lookup failure at import
        return (
            f"Error: the computer tool needs pyautogui ({e}). "
            "Install it with: pip install pyautogui pillow"
        )
    # Its default is to abort on a corner-of-screen mouse position; that turns a
    # legitimate click at (0, 0) into a crash.
    pyautogui.FAILSAFE = False

    try:
        result = _dispatch(pyautogui, action, tool_input)
    except Exception as e:
        return f"Error: {action} failed: {e}"

    if isinstance(result, dict) or action in _NO_SCREENSHOT:
        return result
    time.sleep(_SETTLE)
    shot = _screenshot(pyautogui, caption=f"After {action}.")
    if isinstance(shot, dict):
        return shot
    return f"{result} (screenshot unavailable: {shot})"


def _dispatch(pyautogui, action: str, ti: dict):
    if action == "screenshot":
        return _screenshot(pyautogui, caption="Current screen.")

    if action == "zoom":
        return _zoom(pyautogui, ti.get("region"))

    if action == "wait":
        duration = min(float(ti.get("duration", 1)), 30.0)
        time.sleep(duration)
        return f"Waited {duration}s."

    if action == "mouse_move":
        x, y = _to_native(pyautogui, ti.get("coordinate"))
        pyautogui.moveTo(x, y)
        return f"Moved to {ti['coordinate']}."

    if action in ("left_mouse_down", "left_mouse_up"):
        if ti.get("coordinate"):
            x, y = _to_native(pyautogui, ti["coordinate"])
            pyautogui.moveTo(x, y)
        (pyautogui.mouseDown if action == "left_mouse_down" else pyautogui.mouseUp)()
        return f"{action} done."

    if action in (
        "left_click",
        "right_click",
        "middle_click",
        "double_click",
        "triple_click",
    ):
        return _click(pyautogui, action, ti)

    if action == "left_click_drag":
        start = ti.get("start_coordinate")
        if start:
            sx, sy = _to_native(pyautogui, start)
            pyautogui.moveTo(sx, sy)
        ex, ey = _to_native(pyautogui, ti.get("coordinate"))
        pyautogui.mouseDown()
        pyautogui.moveTo(ex, ey, duration=0.2)
        pyautogui.mouseUp()
        return f"Dragged to {ti['coordinate']}."

    if action == "type":
        text = ti.get("text", "")
        pyautogui.write(text, interval=0.01)
        return f"Typed {len(text)} characters."

    if action == "key":
        keys = _keys(ti.get("text", ""))
        if not keys:
            return "Error: key action needs a `text` key combination"
        pyautogui.hotkey(*keys)
        return f"Pressed {ti.get('text')}."

    if action == "hold_key":
        keys = _keys(ti.get("text", ""))
        duration = min(float(ti.get("duration", 1)), 30.0)
        for k in keys:
            pyautogui.keyDown(k)
        time.sleep(duration)
        for k in reversed(keys):
            pyautogui.keyUp(k)
        return f"Held {ti.get('text')} for {duration}s."

    if action == "scroll":
        return _scroll(pyautogui, ti)

    return f"Error: unsupported action {action!r}"


def _click(pyautogui, action: str, ti: dict) -> str:
    if ti.get("coordinate"):
        x, y = _to_native(pyautogui, ti["coordinate"])
        pyautogui.moveTo(x, y)
    button = {"right_click": "right", "middle_click": "middle"}.get(action, "left")
    clicks = {"double_click": 2, "triple_click": 3}.get(action, 1)
    # On click/scroll actions `text` carries modifiers to hold, not text to type.
    with _modifiers(pyautogui, ti.get("text")):
        pyautogui.click(button=button, clicks=clicks, interval=0.05)
    return f"{action} at {ti.get('coordinate')}."


def _scroll(pyautogui, ti: dict) -> str:
    if ti.get("coordinate"):
        x, y = _to_native(pyautogui, ti["coordinate"])
        pyautogui.moveTo(x, y)
    direction = (ti.get("scroll_direction") or "down").lower()
    amount = int(ti.get("scroll_amount", 3))
    with _modifiers(pyautogui, ti.get("text")):
        if direction in ("up", "down"):
            pyautogui.scroll(amount if direction == "up" else -amount)
        else:
            pyautogui.hscroll(amount if direction == "right" else -amount)
    return f"Scrolled {direction} by {amount}."


class _modifiers:
    """Hold modifier keys for the duration of a click or scroll."""

    def __init__(self, pyautogui, text):
        self.pyautogui = pyautogui
        self.keys = _keys(text or "")

    def __enter__(self):
        for k in self.keys:
            self.pyautogui.keyDown(k)

    def __exit__(self, *exc):
        for k in reversed(self.keys):
            self.pyautogui.keyUp(k)
        return False


def _keys(text: str) -> list[str]:
    parts = [p for p in str(text).replace(" ", "").split("+") if p]
    return [_KEY_ALIASES.get(p.lower(), p.lower()) for p in parts]


def _to_native(pyautogui, coordinate) -> tuple[int, int]:
    """Declared-space coordinate from Claude -> real screen pixels."""
    if not coordinate or len(coordinate) != 2:
        raise ValueError("this action needs a [x, y] coordinate")
    native_w, native_h = pyautogui.size()
    x = round(int(coordinate[0]) * native_w / DISPLAY_WIDTH)
    y = round(int(coordinate[1]) * native_h / DISPLAY_HEIGHT)
    # Clamp: a coordinate slightly outside the declared box is a rounding
    # artefact, not a reason to fail the action.
    return max(0, min(x, native_w - 1)), max(0, min(y, native_h - 1))


def _grab(pyautogui):
    """Native-resolution PIL screenshot, trying each backend in turn."""
    errors = []
    try:
        return pyautogui.screenshot()
    except Exception as e:
        errors.append(f"pyautogui: {e}")
    try:
        from PIL import ImageGrab

        return ImageGrab.grab(xdisplay="")
    except Exception as e:
        errors.append(f"PIL.ImageGrab: {e}")
    try:
        from PIL import Image

        raw = subprocess.run(
            ["import", "-window", "root", "png:-"],
            capture_output=True,
            timeout=20,
            check=True,
        ).stdout
        return Image.open(io.BytesIO(raw))
    except Exception as e:
        errors.append(f"ImageMagick import: {e}")
    raise RuntimeError("; ".join(errors))


def _encode(image, caption: str) -> dict:
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="PNG", optimize=True)
    data = base64.standard_b64encode(buffer.getvalue()).decode("ascii")
    return image_result("image/png", data, caption)


def _screenshot(pyautogui, caption: str) -> str | dict:
    try:
        image = _grab(pyautogui)
    except Exception as e:
        return f"Error: could not capture the screen: {e}"
    # Resize to exactly the declared size. This is what keeps Claude's
    # coordinate space and the declared display size in agreement.
    if image.size != (DISPLAY_WIDTH, DISPLAY_HEIGHT):
        from PIL import Image

        # `Image.Resampling.LANCZOS`, not the older `Image.LANCZOS` alias:
        # the enum is the canonical spelling since Pillow 9.1, and the alias
        # is absent from the type stubs.
        image = image.resize(
            (DISPLAY_WIDTH, DISPLAY_HEIGHT), Image.Resampling.LANCZOS
        )
    return _encode(image, f"{caption} Screen is {DISPLAY_WIDTH}x{DISPLAY_HEIGHT}.")


def _zoom(pyautogui, region) -> str | dict:
    if not region or len(region) != 4:
        raise ValueError("zoom needs a [x1, y1, x2, y2] region")
    try:
        image = _grab(pyautogui)
    except Exception as e:
        return f"Error: could not capture the screen: {e}"

    native_w, native_h = image.size
    x1, y1, x2, y2 = (int(v) for v in region)
    scale_x, scale_y = native_w / DISPLAY_WIDTH, native_h / DISPLAY_HEIGHT
    box = (
        max(0, round(x1 * scale_x)),
        max(0, round(y1 * scale_y)),
        min(native_w, round(x2 * scale_x)),
        min(native_h, round(y2 * scale_y)),
    )
    if box[2] <= box[0] or box[3] <= box[1]:
        return f"Error: empty zoom region {list(region)}"
    crop = image.crop(box)
    return _encode(
        crop,
        f"Zoom of region {list(region)} at native resolution ({crop.width}x{crop.height}).",
    )
