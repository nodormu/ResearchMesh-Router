"""Custom Playwright browser tool — DOM automation (no GUI, no raw HTTP).

Claude learns these tools from their descriptions at runtime. One browser
session is kept alive across tool calls so multi-step flows work
(navigate -> fill -> click -> extract). Launch modes, profiles and downloads
live in core/browser_session.py. Every tool trims what it returns to keep
responses out of firehose territory.

Requires:  pip install playwright  &&  playwright install chromium
Modes `virtual` and `real` also need Google Chrome installed (`virtual` needs
Xvfb as well).
"""

import asyncio
import contextlib
import time
from typing import Any
from urllib.parse import urljoin

from core.browser_session import MODES, PROFILE_NAME, Session, open_session
from core.output import clip
from core.processes import _redact, resolve_secret

TOOLS = [
    {
        "name": "browser_navigate",
        "description": (
            "Open a URL in a browser and return the page title plus its trimmed "
            "visible text. This is the primary way to browse the web: it renders "
            "JavaScript and keeps one live session across calls, so it is the entry "
            "point for surfing a site through the DOM (navigate -> extract -> click / "
            "fill -> navigate). Use it whenever you will read a page and then follow "
            "links, drill into results, or interact. web_fetch is the narrower "
            "alternative: raw text of one known document, no rendering, no session. "
            "A page with a Cloudflare 'verify you are human' check gets a `Human "
            "check:` line; when it says pending or a challenge page, navigate again "
            "with mode `virtual` or `real`. Files the browser downloads are saved to "
            "~/Downloads and listed as `Downloaded:` lines in a tool result."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Absolute URL to open, including http:// or https://.",
                },
                "mode": {
                    "type": "string",
                    "enum": list(MODES),
                    "description": (
                        "headless (default): no window. headed: a visible window. "
                        "virtual: real Chrome on a hidden display, no window; use it "
                        "when a site's human check fails headless. real: the "
                        "installed Chrome started as a normal program, visible and "
                        "the least detectable; the user can click a check in it. "
                        "Leave unset to keep the current mode. Changing mode or "
                        "profile restarts the browser, which drops the open page and "
                        "any login not kept in a profile, so set it on the first "
                        "navigate of a task."
                    ),
                },
                "profile": {
                    "type": "string",
                    "description": (
                        "Name of a persistent profile (letters, digits, '-' or '_'). "
                        "Cookies and logins in it survive restarts. Leave unset for "
                        "a temporary profile that is discarded on close."
                    ),
                },
                "headed": {
                    "type": "boolean",
                    "description": "true is mode headed, false is mode headless. Prefer `mode`.",
                },
            },
            "required": ["url"],
        },
    },
    {
        "name": "browser_extract",
        "description": (
            "Return the text (and href, for links) of elements on the CURRENT page "
            "matching a CSS selector. Use after browser_navigate to pull out specific "
            "content instead of the whole page."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "selector": {
                    "type": "string",
                    "description": "CSS selector, e.g. 'h1', '.price', 'a.result'.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max number of matches to return (default 20).",
                },
            },
            "required": ["selector"],
        },
    },
    {
        "name": "browser_click",
        "description": (
            "Click the first element matching a CSS selector on the current page, "
            "then return the resulting page's title and trimmed text."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "selector": {
                    "type": "string",
                    "description": "CSS selector of the element to click.",
                }
            },
            "required": ["selector"],
        },
    },
    {
        "name": "browser_fill",
        "description": (
            "Fill a form field (input or textarea) matching a CSS selector. Give "
            "exactly one of `value` (literal text; never use it for a password or "
            "token) or `value_secret` (the NAME of a `pass` vault entry; the real "
            "value is decrypted locally, typed into the field, and never appears in "
            "this call or its result). Follow with browser_click to submit."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "selector": {
                    "type": "string",
                    "description": "CSS selector of the input/textarea.",
                },
                "value": {"type": "string", "description": "Text to enter."},
                "value_secret": {
                    "type": "string",
                    "description": (
                        "Name of a `pass` entry to type into the field. Use \"?\" to "
                        "get the list of entries. The user names the exact entry."
                    ),
                },
            },
            "required": ["selector"],
        },
    },
    {
        "name": "browser_links",
        "description": (
            "List the links on the CURRENT page as text + URL pairs. This is how "
            "you decide where to surf next: read the page, list its links, then "
            "navigate to the one you want instead of guessing at a selector."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "contains": {
                    "type": "string",
                    "description": (
                        "Only return links whose text or URL contains this "
                        "(case-insensitive). Omit for all of them."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Max number of links to return (default 50).",
                },
            },
        },
    },
    {
        "name": "browser_back",
        "description": (
            "Go back to the previous page in history and return its title, URL, and "
            "trimmed text. Use this to back out of a dead end while surfing, rather "
            "than re-navigating from the start."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "browser_tab",
        "description": (
            "List, switch between, or close the browser's open tabs. A click that "
            "opens a new tab (a Download button, a link with target=_blank) switches "
            "to it automatically; use this to return to an earlier tab or to close "
            "one you are finished with."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "switch", "close"]},
                "index": {
                    "type": "integer",
                    "description": (
                        "Tab number from `list`, starting at 0. `close` without an "
                        "index closes the current tab."
                    ),
                },
            },
            "required": ["action"],
        },
    },
]

_TOOL_NAMES = {t["name"] for t in TOOLS}
_MAX_TEXT = 6000
_MAX_LINKS = 50

# Opened on first use so importing this module never requires playwright.
_session: Session | None = None
_page: Any = None
# Values typed from the vault, scrubbed from everything this module returns.
_filled_secrets: set[str] = set()


def handles(name: str) -> bool:
    return name in _TOOL_NAMES


def _scrub(text: str) -> str:
    """Scrub vault values from page-derived text. Runs BEFORE clipping: a clip
    can cut a secret in half, and a half-secret no longer matches."""
    return _redact(text, list(_filled_secrets))


def _trim(text: str) -> str:
    """Collapse the whitespace Playwright hands back, then clip to budget.

    For prose (page body text) only — it flattens newlines, so lists of
    elements clip their lines individually and join them afterwards.
    """
    return clip(" ".join(text.split()), _MAX_TEXT)


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _absolute(page_url: str, href: str | None) -> str:
    """Resolve a possibly-relative href, so the URL can be navigated as-is."""
    return urljoin(page_url, href) if href else ""


_CHECK_JS = """() => {
  const field = document.querySelector('[name="cf-turnstile-response"]');
  const widget = field || document.querySelector(
    '.cf-turnstile, iframe[src*="challenges.cloudflare.com"]');
  return {widget: !!widget, token: field ? field.value.length : 0,
          wall: /just a moment|attention required/i.test(document.title)};
}"""


async def _check_state(page) -> dict:
    try:
        return await page.evaluate(_CHECK_JS)
    except Exception:
        return {"widget": False, "token": 0, "wall": False}


async def _human_check(page, wait: float = 8.0) -> str:
    """One line on a Cloudflare human check, or '' when the page has none. A
    check that has not solved yet gets a few seconds to."""
    state = await _check_state(page)
    deadline = time.monotonic() + wait
    while (state["wall"] or (state["widget"] and not state["token"])) and time.monotonic() < deadline:
        await asyncio.sleep(0.5)
        state = await _check_state(page)
    if state["wall"]:
        return "Human check: Cloudflare challenge page is showing. Navigate again with mode `virtual` or `real`."
    if state["widget"] and state["token"]:
        return "Human check: solved."
    if state["widget"]:
        return (
            "Human check: pending, not solved. Navigate again with mode `virtual` or "
            "`real`, or have the user click it in a visible window."
        )
    return ""


async def _page_report(page, prefix: str, extra: str = "") -> str:
    """Title, URL, and trimmed body text — the URL is here so nothing needs a
    separate 'where am I' tool. `extra` is one more header line."""
    check = await _human_check(page)
    title = _scrub(await page.title())
    body = _scrub(await page.inner_text("body"))
    lines = [f"{prefix}: {title}", f"URL: {_scrub(page.url)}"]
    lines += [line for line in (extra, check) if line]
    return "\n".join(lines) + f"\n\n{_trim(body)}"


async def _ensure_page(mode: str | None = None, profile: str | None = None):
    """The live page. `mode` or `profile` None keeps the current one; a
    different value restarts the browser, which drops the open page and any
    login not kept in a profile."""
    global _session, _page
    if _session is not None and (
        (mode is not None and mode != _session.mode)
        or (profile is not None and profile != _session.profile)
    ):
        await shutdown()
    if _session is None:
        _session = await open_session(mode or "headless", profile)
        try:
            _page = await _session.first_page()
        except Exception:
            await shutdown()
            raise
    return _page


def _live_session() -> Session:
    if _session is None:
        raise RuntimeError("browser session is not open")
    return _session


def _session_options(name: str, tool_input: dict):
    """(mode, profile, error) for a browser_navigate call. Checked before the
    browser is touched, so a bad value never drops the open page."""
    if name != "browser_navigate":
        return None, None, None
    mode = tool_input.get("mode")
    headed = tool_input.get("headed")
    profile = tool_input.get("profile")
    if headed is not None:
        if not isinstance(headed, bool):
            return None, None, "Error: `headed` must be true or false"
        if mode is not None:
            return None, None, "Error: give `mode` or `headed`, not both"
        mode = "headed" if headed else "headless"
    if mode is not None and mode not in MODES:
        return None, None, f"Error: `mode` must be one of {', '.join(MODES)}"
    if profile is not None and not PROFILE_NAME.fullmatch(str(profile)):
        return None, None, "Error: `profile` must be 1-40 characters: letters, digits, '-' or '_'"
    return mode, profile, None


async def _follow_new_tab(page):
    """(page, opened): the newest tab a click opened, or the same page."""
    global _page
    session = _live_session()
    opened = [p for p in session.opened_pages if not p.is_closed()]
    session.opened_pages.clear()
    if not opened:
        return page, False
    _page = opened[-1]
    with contextlib.suppress(Exception):
        await _page.wait_for_load_state("domcontentloaded")
    return _page, True


async def _tab_tool(page, tool_input: dict) -> str:
    global _page
    session = _live_session()
    tabs = [p for p in session.context.pages if not p.is_closed()]
    action = tool_input.get("action")
    if action == "list":
        rows = []
        for i, tab in enumerate(tabs):
            mark = "  <- current" if tab is page else ""
            rows.append(f"{i}: {_scrub(await tab.title())}  [{_scrub(tab.url)}]{mark}")
        return clip("\n".join(rows), _MAX_TEXT)
    current = tabs.index(page) if page in tabs else 0
    index = tool_input.get("index", current)
    if action not in ("switch", "close") or not isinstance(index, int) or not 0 <= index < len(tabs):
        return f"Error: need action list, switch or close, and an index from 0 to {len(tabs) - 1}"
    target = tabs[index]
    if action == "switch":
        _page = target
        await target.bring_to_front()
        return await _page_report(target, f"Switched to tab {index}")
    await target.close()
    tabs.remove(target)
    if not tabs:
        tabs.append(await session.context.new_page())
    if target is page:
        _page = tabs[min(index, len(tabs) - 1)]
    return await _page_report(_page, f"Closed tab {index}. Now on")


async def _download_notes() -> str:
    """`Downloaded:` lines for files that arrived since the last tool result."""
    if _session is None:
        return ""
    files = await _session.new_downloads()
    return "".join(f"\nDownloaded: {f} ({f.stat().st_size} bytes)" for f in files)


async def execute(name: str, tool_input: dict) -> str:
    result = await _dispatch(name, tool_input)
    result += await _download_notes()
    return _redact(result, list(_filled_secrets))


async def _dispatch(name: str, tool_input: dict) -> str:
    try:
        mode, profile, error = _session_options(name, tool_input)
        if error:
            return error
        page = await _ensure_page(mode, profile)

        if name == "browser_navigate":
            await page.goto(tool_input["url"], wait_until="domcontentloaded")
            return await _page_report(page, "Title", f"Mode: {_live_session().describe()}")

        if name == "browser_extract":
            selector = tool_input["selector"]
            limit = int(tool_input.get("limit", 20))
            elements = await page.query_selector_all(selector)
            out = []
            for el in elements[:limit]:
                text = _one_line(await el.inner_text())
                href = await el.get_attribute("href")
                out.append(text + (f"  [{_absolute(page.url, href)}]" if href else ""))
            if not out:
                return f"No elements matched selector {selector!r}"
            # clip, not _trim: _trim would flatten these lines into one.
            return clip(_scrub("\n".join(out)), _MAX_TEXT)

        if name == "browser_click":
            _live_session().opened_pages.clear()
            await page.click(tool_input["selector"])
            await asyncio.sleep(0.7)  # a click can open a tab or start a download
            page, opened = await _follow_new_tab(page)
            await page.wait_for_load_state("domcontentloaded")
            return await _page_report(page, "Clicked. Opened a new tab. Now on" if opened else "Clicked. Now on")

        if name == "browser_fill":
            selector = tool_input["selector"]
            if ("value" in tool_input) == ("value_secret" in tool_input):
                return "Error: browser_fill needs exactly one of `value` or `value_secret`"
            if "value_secret" in tool_input:
                entry = str(tool_input["value_secret"])
                secret, error = await asyncio.to_thread(resolve_secret, entry)
                if error:
                    return error
                if not secret:
                    return f"Error: vault entry {entry!r} returned an empty value"
                _filled_secrets.add(secret)
                await page.fill(selector, secret)
                return f"Filled {selector!r} from vault entry {entry!r} (value not shown)"
            await page.fill(selector, tool_input["value"])
            return f"Filled {selector!r}"

        if name == "browser_links":
            needle = (tool_input.get("contains") or "").lower()
            limit = int(tool_input.get("limit", _MAX_LINKS))
            out = []
            for el in await page.query_selector_all("a[href]"):
                href = await el.get_attribute("href")
                text = _one_line(await el.inner_text())
                if needle and needle not in text.lower() and needle not in (href or "").lower():
                    continue
                out.append(f"{text or '(no text)'}  ->  {_absolute(page.url, href)}")
                if len(out) >= limit:
                    break
            if not out:
                where = f" matching {needle!r}" if needle else ""
                return f"No links{where} on {page.url}"
            return clip(_scrub(f"Links on {page.url}:\n" + "\n".join(out)), _MAX_TEXT)

        if name == "browser_back":
            if await page.go_back(wait_until="domcontentloaded") is None:
                return f"Nothing to go back to; still on {page.url}"
            return await _page_report(page, "Went back. Now on")

        if name == "browser_tab":
            return await _tab_tool(page, tool_input)

        return f"Error: unknown browser tool {name!r}"

    except Exception as e:
        return f"Browser error in {name}: {e}"


async def shutdown():
    """Close the browser session. Safe to call even if never launched."""
    global _session, _page
    session, _session, _page = _session, None, None
    if session is not None:
        await session.close()
    _filled_secrets.clear()
