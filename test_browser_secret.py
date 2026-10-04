"""Behavioural tests for `browser_fill` `value_secret` in core/browser.py.

    python test_browser_secret.py

Drives the real `browser.execute` against a local login form in headless
Chromium. A fake `pass` on PATH supplies the secret. The server records what
the form posts and reflects the password back in its response page, so the
checks cover both directions: the real value reaches the page, and no tool
result contains it.
"""

import asyncio
import html
import os
import stat
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

FAILURES: list[str] = []
SECRET = "Pw!x&y=1 z#9"
RECEIVED: dict[str, str] = {}

LOGIN_PAGE = b"""<html><body><form method="post" action="/login">
<input id="user" name="user"><input id="pw" name="pw" type="password">
<button id="go" type="submit">Sign in</button></form></body></html>"""


GET_FORM_PAGE = b"""<html><body><form method="get" action="/got">
<input id="pw2" name="pw" type="password"><button id="go2" type="submit">Go</button>
</form></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/getform"):
            self._send(GET_FORM_PAGE)
        elif self.path.startswith("/got"):
            self._send(b"<html><body>received</body></html>")
        elif self.path.startswith("/long"):
            # The secret begins 5 characters before the 6000-character clip limit.
            self._send(f"<html><body>{'x' * 5995} {html.escape(SECRET)}</body></html>".encode())
        else:
            self._send(LOGIN_PAGE)

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"])).decode()
        form = {k: v[0] for k, v in parse_qs(body).items()}
        RECEIVED.update(form)
        page = f"<html><body>Welcome {html.escape(form.get('user', ''))}. pw={html.escape(form.get('pw', ''))}</body></html>"
        self._send(page.encode())

    def _send(self, payload: bytes):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


PASS_SCRIPT = f"""#!/bin/sh
if [ "$1" = "show" ]; then
    case "$2" in
        web-login) printf '%s\\n' '{SECRET}'; exit 0 ;;
        empty-entry) exit 0 ;;
        *) echo "Error: $2 is not in the password store." >&2; exit 1 ;;
    esac
fi
exit 1
"""


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name}{' — ' + detail if detail else ''}")
        FAILURES.append(name)


async def main_async(mod, proc, base: str) -> None:
    results: list[str] = []

    async def run(name: str, tool_input: dict) -> str:
        out = await mod.execute(name, tool_input)
        results.append(out)
        return out

    await run("browser_navigate", {"url": f"{base}/login"})

    print("literal value still works")
    out = await run("browser_fill", {"selector": "#user", "value": "alice"})
    check("literal fill reports success", out == "Filled '#user'", out)

    print("exactly one of value / value_secret")
    out = await run("browser_fill", {"selector": "#pw"})
    check("neither is rejected", out.startswith("Error: browser_fill needs exactly one"), out)
    out = await run("browser_fill", {"selector": "#pw", "value": "x", "value_secret": "web-login"})
    check("both is rejected", out.startswith("Error: browser_fill needs exactly one"), out)

    print("name gate: only an entry the user typed can be decrypted")
    out = await run("browser_fill", {"selector": "#pw", "value_secret": "web-login"})
    check("an untyped name returns the selection prompt",
          out.startswith("please select the cred name I need to use:") and "web-login" in out, out)
    out = await run("browser_fill", {"selector": "#pw", "value_secret": "web-login"})
    check("naming it again from the model side is still refused",
          out.startswith("please select the cred name I need to use:"), out)
    proc.note_user_message("log me in please")
    proc.note_user_message("use web-login-old for it")
    out = await run("browser_fill", {"selector": "#pw", "value_secret": "web-login"})
    check("a message without the exact name, or with a longer name, does not confirm it",
          out.startswith("please select the cred name I need to use:"), out)

    print("missing and empty entries fail clearly")
    proc.note_user_message("also try no-such-entry and empty-entry")
    out = await run("browser_fill", {"selector": "#pw", "value_secret": "no-such-entry"})
    check("missing entry names the failure", "failed" in out and "no-such-entry" in out, out)
    out = await run("browser_fill", {"selector": "#pw", "value_secret": "empty-entry"})
    check("empty entry is rejected", "empty value" in out, out)

    print("the user typing the name confirms it")
    proc.note_user_message("use web-login.")

    print("confirmed entry fills the page")
    out = await run("browser_fill", {"selector": "#pw", "value_secret": "web-login"})
    check("result says value not shown",
          out == "Filled '#pw' from vault entry 'web-login' (value not shown)", out)

    out = await run("browser_click", {"selector": "#go"})
    check("page loaded after submit", "Welcome alice" in out, out)
    check("server received the real password", RECEIVED.get("pw") == SECRET, repr(RECEIVED))
    check("server received the literal username", RECEIVED.get("user") == "alice", repr(RECEIVED))
    check("reflected password is scrubbed from the page report", "pw=***" in out, out)

    print("no tool result ever contains the secret")
    out = await run("browser_extract", {"selector": "body"})
    results.append(out)
    check("secret absent from every returned string", not any(SECRET in r for r in results))

    print("encoded forms: a GET form puts the password in the page URL")
    from urllib.parse import quote, quote_plus

    await run("browser_navigate", {"url": f"{base}/getform"})
    await run("browser_fill", {"selector": "#pw2", "value_secret": "web-login"})
    out = await run("browser_click", {"selector": "#go2"})
    check("URL is reported", f"URL: {base}/got?pw=" in out, out)
    check("percent-encoded secret is scrubbed from the URL",
          quote_plus(SECRET) not in out and quote(SECRET, safe="") not in out, out)
    check("URL shows the redaction marker", f"{base}/got?pw=***" in out, out)

    print("clip boundary: a secret straddling the clip limit leaves no prefix")
    out = await run("browser_navigate", {"url": f"{base}/long"})
    check("no leading fragment of the secret survives the clip", SECRET[:4] not in out, out[-60:])
    check("marker present", "***" in out, out[-60:])

    print("failed fill: error text is scrubbed too")
    mod._page.set_default_timeout(2000)
    out = await run("browser_fill", {"selector": "#does-not-exist", "value_secret": "web-login"})
    check("fill error is reported", out.startswith("Browser error in browser_fill"), out)
    check("fill error does not contain the secret", SECRET not in out, out)

    print("shutdown clears remembered secrets")
    await mod.shutdown()
    check("remembered secrets cleared", not mod._filled_secrets)


def main() -> int:
    import core.browser as mod
    import core.processes as proc

    tmp = tempfile.mkdtemp()
    pass_path = os.path.join(tmp, "pass")
    with open(pass_path, "w") as f:
        f.write(PASS_SCRIPT)
    os.chmod(pass_path, os.stat(pass_path).st_mode | stat.S_IEXEC)
    store = os.path.join(tmp, "store")
    os.makedirs(store)
    for entry in ("web-login", "web-login-old", "no-such-entry", "empty-entry"):
        open(os.path.join(store, f"{entry}.gpg"), "w").close()
    old_path, old_store = os.environ["PATH"], os.environ.get("PASSWORD_STORE_DIR")
    os.environ["PATH"] = tmp + os.pathsep + old_path
    os.environ["PASSWORD_STORE_DIR"] = store
    proc._confirmed_secret_entries.clear()

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        asyncio.run(main_async(mod, proc, f"http://127.0.0.1:{server.server_port}"))
    finally:
        server.shutdown()
        os.environ["PATH"] = old_path
        if old_store is None:
            os.environ.pop("PASSWORD_STORE_DIR", None)
        else:
            os.environ["PASSWORD_STORE_DIR"] = old_store

    total = len(FAILURES)
    print(f"\n{'FAILED' if total else 'all checks passed'}"
          + (f": {total} failure(s)" if total else ""))
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
