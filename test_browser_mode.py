"""Behavioural tests for `browser_navigate` `headed` in core/browser.py.

    xvfb-run -a python test_browser_mode.py

Headed mode needs a display. Under xvfb-run the window is on a virtual screen.
Run directly with a real DISPLAY or WAYLAND_DISPLAY set, a window opens on that
desktop. With neither set, the headed steps are skipped.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

FAILURES: list[str] = []
PAGE = "data:text/html,<title>t</title>hello"
DISPLAY_VARS = ("DISPLAY", "WAYLAND_DISPLAY")


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name}{' — ' + detail if detail else ''}")
        FAILURES.append(name)


async def user_agent(mod) -> str:
    return await mod._page.evaluate("navigator.userAgent")


async def main_async(mod) -> None:
    have_display = any(os.environ.get(v) for v in DISPLAY_VARS)

    print("default is headless")
    out = await mod.execute("browser_navigate", {"url": PAGE})
    check("navigate works", out.startswith("Title: t"), out)
    check("mode flag is headless", mod._headless is True)
    check("user agent says headless", "Headless" in await user_agent(mod))

    print("a non-boolean `headed` is rejected without touching the browser")
    page_before = mod._page
    out = await mod.execute("browser_navigate", {"url": PAGE, "headed": "false"})
    check("string value is an error", out == "Error: `headed` must be true or false", out)
    check("browser untouched", mod._page is page_before and mod._headless is True)

    if not have_display:
        print("no display available: headed steps skipped")
        await mod.shutdown()
        return

    print("headed: true restarts the browser in a visible window")
    out = await mod.execute("browser_navigate", {"url": PAGE, "headed": True})
    check("navigate works headed", out.startswith("Title: t"), out)
    check("mode flag is headed", mod._headless is False)
    check("a new page replaced the old one", mod._page is not page_before)
    check("user agent no longer says headless", "Headless" not in await user_agent(mod))

    print("leaving `headed` unset keeps the current mode")
    page_headed = mod._page
    await mod.execute("browser_navigate", {"url": PAGE})
    check("still headed, same page object", mod._headless is False and mod._page is page_headed)

    print("headed: false goes back to headless")
    await mod.execute("browser_navigate", {"url": PAGE, "headed": False})
    check("mode flag is headless", mod._headless is True)
    check("user agent says headless again", "Headless" in await user_agent(mod))

    print("a failed headed launch is reported and the next call recovers")
    saved = {v: os.environ.pop(v) for v in DISPLAY_VARS if v in os.environ}
    try:
        out = await mod.execute("browser_navigate", {"url": PAGE, "headed": True})
    finally:
        os.environ.update(saved)
    check("error is reported", out.startswith("Browser error in browser_navigate"), out[:120])
    check("no half-started browser is left", mod._page is None and mod._playwright is None)
    out = await mod.execute("browser_navigate", {"url": PAGE})
    check("next call relaunches headless", out.startswith("Title: t") and mod._headless is True, out)

    await mod.shutdown()


def main() -> int:
    import core.browser as mod

    asyncio.run(main_async(mod))
    total = len(FAILURES)
    print(f"\n{'FAILED' if total else 'all checks passed'}"
          + (f": {total} failure(s)" if total else ""))
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
