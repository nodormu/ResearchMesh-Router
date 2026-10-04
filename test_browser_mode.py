"""Behavioural tests for browser modes, profiles, tabs and downloads.

    xvfb-run -a python test_browser_mode.py

Drives the real `browser.execute` against a local server. Headed and real modes
need a display: under xvfb-run it is a virtual screen, run directly a window
opens on that desktop, and with none those steps are skipped. Virtual mode needs
Xvfb and installed Google Chrome.
"""

import asyncio
import json
import os
import shutil
import stat
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

WORK = Path(tempfile.mkdtemp(prefix="rm-browser-test-"))
os.environ["XDG_CACHE_HOME"] = str(WORK / "cache")
os.environ["RESEARCHMESH_DOWNLOAD_DIR"] = str(WORK / "downloads")

FAILURES: list[str] = []
DISPLAY_VARS = ("DISPLAY", "WAYLAND_DISPLAY")
PAGES = {
    "/": b'<title>home</title><a id="tab" href="/second" target="_blank">s</a>'
         b'<a id="dl" href="/file">f</a>',
    "/second": b"<title>second</title>second page",
    "/wall": b"<title>Just a moment...</title>checking",
    "/solved": b'<title>c</title><input name="cf-turnstile-response" value="">'
               b"<script>setTimeout(()=>{document.querySelector('[name=cf-turnstile-response]')"
               b".value='t'.repeat(40)},700)</script>",
    "/pending": b'<title>p</title><input name="cf-turnstile-response" value="">',
}


SERVED = [0]


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/file":
            SERVED[0] += 1
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", 'attachment; filename="x.bin"')
            self.send_header("Content-Length", "10")
            self.end_headers()
            self.wfile.write(f"{SERVED[0]:010d}".encode())
            return
        body = PAGES.get(self.path, PAGES["/"])
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name}{' — ' + detail if detail else ''}")
        FAILURES.append(name)


async def js(mod, expression: str):
    return await mod._page.evaluate(expression)


async def main_async(mod, sess, base: str) -> None:
    chrome = sess.find_chrome()
    have_display = any(os.environ.get(v) for v in DISPLAY_VARS)
    downloads = WORK / "downloads"

    print("default is headless, and the automation flag is off")
    out = await mod.execute("browser_navigate", {"url": base})
    check("navigate works", out.startswith("Title: home"), out)
    check("report names the mode", "Mode: headless" in out, out[:160])
    check("session mode is headless", mod._session.mode == "headless")
    check("navigator.webdriver is false", await js(mod, "navigator.webdriver") is False)
    agent = await js(mod, "navigator.userAgent")
    check("headless marker only without installed Chrome", ("Headless" in agent) == (chrome is None), agent)

    print("bad options are rejected without touching the browser")
    page_before = mod._page
    for label, call, expected in (
        ("string headed", {"headed": "false"}, "Error: `headed` must be true or false"),
        ("mode and headed", {"mode": "headed", "headed": True}, "Error: give `mode` or `headed`, not both"),
        ("unknown mode", {"mode": "stealth"}, "Error: `mode` must be one of headless, headed, virtual, real"),
        ("bad profile", {"profile": "../x"}, "Error: `profile` must be 1-40 characters: letters, digits, '-' or '_'"),
    ):
        out = await mod.execute("browser_navigate", {"url": base, **call})
        check(f"{label} is an error", out == expected, out)
    check("browser untouched", mod._page is page_before and mod._session.mode == "headless")

    print("the same mode keeps the page; a different one restarts the browser")
    await mod.execute("browser_navigate", {"url": base, "mode": "headless"})
    check("same mode, same page object", mod._page is page_before)

    print("human check line")
    out = await mod.execute("browser_navigate", {"url": base + "/solved"})
    check("a check that solves is reported solved", "Human check: solved." in out, out[:200])
    await mod.execute("browser_navigate", {"url": base + "/pending"})
    pending = await mod._human_check(mod._page, wait=0.5)
    check("a check that never solves is reported pending", pending.startswith("Human check: pending"), pending)
    await mod.execute("browser_navigate", {"url": base + "/wall"})
    wall = await mod._human_check(mod._page, wait=0.5)
    check("a challenge page is reported", wall.startswith("Human check: Cloudflare challenge page"), wall)
    out = await mod.execute("browser_navigate", {"url": base + "/second"})
    check("a page without a check has no line", "Human check" not in out, out[:200])

    print("a fresh default-mode visit stopped by a check is reopened in virtual mode")
    mod._CHECK_WAIT = 1.0
    await mod.shutdown()
    out = await mod.execute("browser_navigate", {"url": base + "/pending"})
    if chrome and shutil.which("Xvfb"):
        check("reopened in virtual mode", "Mode: virtual" in out and "reopened from headless" in out, out[:260])
        check("the session is now virtual", mod._session.mode == "virtual")
        check("the hint points at real mode", "Navigate again with mode `real`" in out, out[:400])
    else:
        check("without Chrome and Xvfb it stays headless", "Mode: headless" in out and "Human check: pending" in out, out[:260])
    await mod.shutdown()
    out = await mod.execute("browser_navigate", {"url": base + "/pending", "mode": "headless"})
    check("an explicit mode is never reopened", "Mode: headless" in out and "reopened" not in out, out[:200])
    out = await mod.execute("browser_navigate", {"url": base + "/pending"})
    check("an open session is never reopened", "Mode: headless" in out and "reopened" not in out, out[:200])
    await mod.shutdown()
    out = await mod.execute("browser_navigate", {"url": base + "/second"})
    check("a page with no check stays headless", "Mode: headless" in out and "reopened" not in out, out[:200])
    saved_find = mod.find_chrome
    mod.find_chrome = lambda: None
    try:
        check("no installed Chrome means no reopen", await mod._reopen_virtual(base) is None)
    finally:
        mod.find_chrome = saved_find
    check("and the open session is untouched", mod._session is not None and mod._session.mode == "headless")
    mod._CHECK_WAIT = 8.0

    print("a click that opens a tab switches to it; browser_tab lists, switches, closes")
    await mod.execute("browser_navigate", {"url": base})
    out = await mod.execute("browser_click", {"selector": "#tab"})
    check("new tab is followed", out.startswith("Clicked. Opened a new tab. Now on: second"), out[:120])
    listing = await mod.execute("browser_tab", {"action": "list"})
    rows = listing.splitlines()
    check("two tabs listed, second is current", len(rows) == 2 and "<- current" in rows[1], listing)
    out = await mod.execute("browser_tab", {"action": "switch", "index": 0})
    check("switch goes to tab 0", out.startswith("Switched to tab 0: home"), out[:80])
    out = await mod.execute("browser_tab", {"action": "close", "index": 1})
    check("close removes the other tab", out.startswith("Closed tab 1. Now on: home"), out[:80])
    check("one tab left", len(mod._session.context.pages) == 1)
    out = await mod.execute("browser_tab", {"action": "switch", "index": 9})
    check("bad index is an error", out.startswith("Error:"), out)

    print("downloads are saved and reported")
    out = await mod.execute("browser_click", {"selector": "#dl"})
    saved = downloads / "x.bin"
    check("report lists the file", f"Downloaded: {saved} (10 bytes)" in out, out[-160:])
    check("file content is right", saved.exists() and saved.read_bytes() == b"0000000001")

    print("a profile keeps cookies across restarts and is private")
    await mod.execute("browser_navigate", {"url": base, "profile": "tprof"})
    await js(mod, "document.cookie = 'k=v; max-age=3600'")
    pdir = sess.profile_root() / "tprof"
    await mod.shutdown()
    check("profile dir is mode 700", stat.S_IMODE(pdir.stat().st_mode) == 0o700)
    await mod.execute("browser_navigate", {"url": base, "profile": "tprof"})
    check("cookie survived the restart", "k=v" in await js(mod, "document.cookie"))
    await mod.execute("browser_navigate", {"url": base, "profile": "other"})
    check("another profile starts empty", "k=v" not in await js(mod, "document.cookie"))
    await mod.shutdown()
    await mod.execute("browser_navigate", {"url": base})
    check("no profile starts empty", "k=v" not in await js(mod, "document.cookie"))

    print("virtual mode: Chrome on a hidden display")
    if chrome and shutil.which("Xvfb"):
        out = await mod.execute("browser_navigate", {"url": base, "mode": "virtual"})
        check("navigate works in virtual mode", out.startswith("Title: home") and "Mode: virtual" in out, out[:160])
        check("webdriver is false", await js(mod, "navigator.webdriver") is False)
        xvfb = mod._session._procs[0]
        check("Xvfb is running", xvfb.returncode is None)
        await mod.shutdown()
        check("Xvfb stopped on shutdown", xvfb.returncode is not None)
    else:
        print("  skipped: needs Xvfb and installed Google Chrome")

    if have_display:
        print("headed: true is the headed mode")
        out = await mod.execute("browser_navigate", {"url": base, "headed": True})
        check("navigate works headed", out.startswith("Title: home") and "Mode: headed" in out, out[:160])
        check("session mode is headed", mod._session.mode == "headed")
        agent = await js(mod, "navigator.userAgent")
        check("user agent has no headless marker", "Headless" not in agent, agent)

        if chrome:
            print("real mode: installed Chrome over CDP")
            await mod.execute("browser_navigate", {"url": base, "mode": "headless"})
            out = await mod.execute("browser_navigate", {"url": base, "mode": "real", "profile": "treal"})
            check("navigate works in real mode", out.startswith("Title: home") and "Mode: real" in out, out[:200])
            check("webdriver is false", await js(mod, "navigator.webdriver") is False)
            out = await mod.execute("browser_click", {"selector": "#dl"})
            check("download reported over CDP", "Downloaded:" in out and "x (1).bin" in out, out[-200:])
            check("an earlier file of the same name is not overwritten", saved.read_bytes() == b"0000000001")
            check("the new file has its own content", (downloads / "x (1).bin").read_bytes() == b"0000000002")
            proc = mod._session._procs[0]
            await mod.shutdown()
            check("Chrome exited on shutdown", proc.returncode is not None)
            prefs = json.loads((sess.profile_root() / "treal" / "Default" / "Preferences").read_text())
            check("new profile has password saving off", prefs.get("credentials_enable_service") is False)
            await mod.execute("browser_navigate", {"url": base, "mode": "real"})
            tmpdir = mod._session._tmpdir
            check("a profile-less real session uses a temporary dir", tmpdir is not None and tmpdir.exists())
            await mod.shutdown()
            check("temporary dir removed on shutdown", tmpdir is not None and not tmpdir.exists())
    else:
        print("no display available: headed and real steps skipped")

    print("a failed launch is reported and the next call recovers")
    saved_env = {v: os.environ.pop(v) for v in DISPLAY_VARS if v in os.environ}
    try:
        out = await mod.execute("browser_navigate", {"url": base, "mode": "headed"})
    finally:
        os.environ.update(saved_env)
    check("error is reported", out.startswith("Browser error in browser_navigate"), out[:120])
    check("no half-started browser is left", mod._session is None and mod._page is None)
    out = await mod.execute("browser_navigate", {"url": base})
    check("next call relaunches headless", out.startswith("Title: home") and mod._session.mode == "headless", out)

    await mod.shutdown()


def main() -> int:
    import core.browser as mod
    import core.browser_session as sess

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        asyncio.run(main_async(mod, sess, f"http://127.0.0.1:{server.server_port}"))
    finally:
        server.shutdown()
        shutil.rmtree(WORK, ignore_errors=True)
    total = len(FAILURES)
    print(f"\n{'FAILED' if total else 'all checks passed'}"
          + (f": {total} failure(s)" if total else ""))
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
