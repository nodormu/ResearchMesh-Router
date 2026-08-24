"""Custom Playwright browser tool — DOM automation (no GUI, no raw HTTP).

Claude learns these tools from their descriptions at runtime. A single headless
browser page is kept alive across tool calls so multi-step flows work
(navigate -> fill -> click -> extract). Every tool trims what it returns to
keep responses out of firehose territory.

Requires:  pip install playwright  &&  playwright install chromium
"""

from urllib.parse import urljoin

from core.output import clip

TOOLS = [
    {
        "name": "browser_navigate",
        "description": (
            "Open a URL in a headless browser and return the page title plus its "
            "trimmed visible text. This is the primary way to browse the web: it "
            "renders JavaScript and keeps one live page across calls, so it is the "
            "entry point for surfing a site through the DOM (navigate -> extract -> "
            "click / fill -> navigate). Use it whenever you will read a page and then "
            "follow links, drill into results, or interact. web_fetch is the narrower "
            "alternative: raw text of one known document, no rendering, no session."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Absolute URL to open, including http:// or https://.",
                }
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
            "Fill a form field (input or textarea) matching a CSS selector with the "
            "given value. Follow with browser_click to submit."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "selector": {
                    "type": "string",
                    "description": "CSS selector of the input/textarea.",
                },
                "value": {"type": "string", "description": "Text to enter."},
            },
            "required": ["selector", "value"],
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
]

_TOOL_NAMES = {t["name"] for t in TOOLS}
_MAX_TEXT = 6000
_MAX_LINKS = 50

# Lazily-initialised singletons so importing this module never requires
# playwright to be installed until a browser tool is actually used.
_playwright = None
_browser = None
_page = None


def handles(name: str) -> bool:
    return name in _TOOL_NAMES


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


async def _page_report(page, prefix: str) -> str:
    """Title, URL, and trimmed body text — the URL is here so nothing needs a
    separate 'where am I' tool."""
    title = await page.title()
    body = await page.inner_text("body")
    return f"{prefix}: {title}\nURL: {page.url}\n\n{_trim(body)}"


async def _ensure_page():
    global _playwright, _browser, _page
    if _page is None:
        from playwright.async_api import async_playwright

        _playwright = await async_playwright().start()
        _browser = await _playwright.chromium.launch(headless=True)
        _page = await _browser.new_page()
    return _page


async def execute(name: str, tool_input: dict) -> str:
    try:
        page = await _ensure_page()

        if name == "browser_navigate":
            await page.goto(tool_input["url"], wait_until="domcontentloaded")
            return await _page_report(page, "Title")

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
            return clip("\n".join(out), _MAX_TEXT)

        if name == "browser_click":
            await page.click(tool_input["selector"])
            await page.wait_for_load_state("domcontentloaded")
            return await _page_report(page, "Clicked. Now on")

        if name == "browser_fill":
            await page.fill(tool_input["selector"], tool_input["value"])
            return f"Filled {tool_input['selector']!r}"

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
            return clip(f"Links on {page.url}:\n" + "\n".join(out), _MAX_TEXT)

        if name == "browser_back":
            if await page.go_back(wait_until="domcontentloaded") is None:
                return f"Nothing to go back to; still on {page.url}"
            return await _page_report(page, "Went back. Now on")

        return f"Error: unknown browser tool {name!r}"

    except Exception as e:
        return f"Browser error in {name}: {e}"


async def shutdown():
    """Close the headless browser. Safe to call even if never launched."""
    global _playwright, _browser, _page
    if _browser is not None:
        await _browser.close()
    if _playwright is not None:
        await _playwright.stop()
    _playwright = _browser = _page = None
