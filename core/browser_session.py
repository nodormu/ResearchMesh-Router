"""Browser lifecycle for core/browser.py: launch modes, profiles, downloads.

Modes (Chromium only):
  headless  no window. Installed Google Chrome when present (user agent
            corrected), else bundled Chromium.
  headed    visible window on the user's desktop.
  virtual   headed on a private Xvfb display, so no window appears. Needs
            installed Chrome and Xvfb.
  real      installed Chrome started as a normal program and attached over CDP
            on 127.0.0.1. No automation flags: the least detectable.

`profile` names a persistent profile directory, so cookies and logins survive.
Without it every session is temporary. Profile directories are mode 700 under
$XDG_CACHE_HOME/researchmesh/browser-profiles.
"""

import asyncio
import contextlib
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path
from typing import Any

MODES = ("headless", "headed", "virtual", "real")
# Playwright launches set navigator.webdriver to true; this turns it off.
LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled"]
DOWNLOAD_DIR = Path(os.environ.get("RESEARCHMESH_DOWNLOAD_DIR", "~/Downloads")).expanduser()
PROFILE_NAME = re.compile(r"[A-Za-z0-9_-]{1,40}")
_PARTIAL = (".crdownload", ".tmp")


def profile_root() -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME") or "~/.cache").expanduser()
    return base / "researchmesh" / "browser-profiles"


def profile_dir(name: str) -> Path:
    if not PROFILE_NAME.fullmatch(name):
        raise ValueError("profile must be 1-40 characters: letters, digits, '-' or '_'")
    root = profile_root()
    path = root / name
    path.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    path.chmod(0o700)
    return path


def find_chrome() -> str | None:
    for name in ("google-chrome", "google-chrome-stable"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _chrome_user_agent(chrome: str) -> str | None:
    """The user agent a normal Chrome sends, without the 'Headless' marker."""
    try:
        out = subprocess.run([chrome, "--version"], capture_output=True, text=True, timeout=10, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"(\d+)\.\d+\.\d+\.\d+", out)
    if not match:
        return None
    return (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{match.group(1)}.0.0.0 Safari/537.36"
    )


def _require_display(mode: str) -> None:
    """Chrome with no display quietly runs windowless; a visible mode must not."""
    if not any(os.environ.get(v) for v in ("DISPLAY", "WAYLAND_DISPLAY")):
        raise RuntimeError(
            f"mode {mode} needs a display (DISPLAY or WAYLAND_DISPLAY is not set); "
            "use mode virtual for a hidden display"
        )


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _seed_preferences(pdir: Path) -> None:
    """A new profile starts with password saving off, so no save prompt appears."""
    prefs = pdir / "Default" / "Preferences"
    if prefs.exists():
        return
    prefs.parent.mkdir(parents=True, exist_ok=True)
    prefs.write_text(json.dumps({
        "credentials_enable_service": False,
        "profile": {"password_manager_enabled": False},
    }))


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    for n in range(1, 1000):
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(str(path))


def _downloads_present() -> set[str]:
    return {p.name for p in DOWNLOAD_DIR.iterdir()} if DOWNLOAD_DIR.is_dir() else set()


class Session:
    def __init__(self, mode: str, profile: str | None):
        self.mode = mode
        self.profile = profile
        self.detail = ""
        self.playwright: Any = None
        self.browser: Any = None
        self.context: Any = None
        self.opened_pages: list[Any] = []
        self._procs: list[asyncio.subprocess.Process] = []
        self._tmpdir: Path | None = None
        self._staging: Path | None = None
        self._tasks: set[asyncio.Task] = set()
        self._known_downloads: set[str] = _downloads_present()

    def describe(self) -> str:
        profile = f", profile {self.profile!r}" if self.profile else ""
        return f"{self.mode} ({self.detail}{profile})"

    async def first_page(self):
        return self.context.pages[0] if self.context.pages else await self.context.new_page()

    async def _open_playwright(self) -> None:
        chrome = find_chrome()
        args = list(LAUNCH_ARGS)
        env = None
        if self.mode == "headed":
            _require_display(self.mode)
        if self.mode == "virtual":
            if not chrome:
                raise RuntimeError("virtual mode needs Google Chrome installed")
            display = await self._start_xvfb()
            env = {k: v for k, v in os.environ.items() if k != "WAYLAND_DISPLAY"}
            env.update(DISPLAY=display, XDG_SESSION_TYPE="x11")
            args.append("--ozone-platform=x11")
        launch: dict = {"headless": self.mode == "headless", "args": args}
        if chrome:
            launch["channel"] = "chrome"
        if env:
            launch["env"] = env
        context_opts: dict = {"accept_downloads": True}
        if self.mode == "headless" and chrome:
            agent = await asyncio.to_thread(_chrome_user_agent, chrome)
            if agent:
                context_opts["user_agent"] = agent
        self.detail = "installed Chrome" if chrome else "bundled Chromium"
        chromium = self.playwright.chromium
        if self.profile:
            self.context = await chromium.launch_persistent_context(
                str(profile_dir(self.profile)), **launch, **context_opts
            )
        else:
            self.browser = await chromium.launch(**launch)
            self.context = await self.browser.new_context(**context_opts)

    async def _start_xvfb(self) -> str:
        xvfb = shutil.which("Xvfb")
        if not xvfb:
            raise RuntimeError("virtual mode needs Xvfb (apt install xvfb)")
        read_fd, write_fd = os.pipe()
        proc = await asyncio.create_subprocess_exec(
            xvfb, "-displayfd", str(write_fd), "-screen", "0", "1920x1080x24", "-nolisten", "tcp",
            pass_fds=(write_fd,), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self._procs.append(proc)
        os.close(write_fd)
        try:
            line = await asyncio.wait_for(asyncio.to_thread(os.read, read_fd, 16), 10)
        except (TimeoutError, OSError):
            line = b""
        finally:
            if not line:
                proc.terminate()
            os.close(read_fd)
        number = line.decode().strip()
        if not number.isdigit():
            raise RuntimeError("Xvfb did not start")
        return f":{number}"

    async def _open_real(self) -> None:
        chrome = find_chrome()
        if not chrome:
            raise RuntimeError("real mode needs Google Chrome installed")
        _require_display(self.mode)
        if self.profile:
            pdir = profile_dir(self.profile)
        else:
            self._tmpdir = pdir = Path(tempfile.mkdtemp(prefix="rm-chrome-"))
        _seed_preferences(pdir)
        port = _free_port()
        proc = await asyncio.create_subprocess_exec(
            chrome, f"--remote-debugging-port={port}", "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={pdir}", "--no-first-run", "--no-default-browser-check",
            "--window-size=1400,950", "about:blank",
            start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self._procs.append(proc)
        for _ in range(60):
            if proc.returncode is not None:
                raise RuntimeError(
                    "Chrome exited at startup; the profile may already be open in another Chrome"
                )
            try:
                self.browser = await self.playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
                break
            except Exception:
                await asyncio.sleep(0.4)
        else:
            raise RuntimeError("Chrome did not open its debug port")
        self.context = self.browser.contexts[0]
        # Chrome overwrites a same-name file when told where to save, so it
        # writes to a private staging dir and new_downloads() moves each
        # finished file into DOWNLOAD_DIR under a unique name.
        self._staging = Path(tempfile.mkdtemp(prefix="rm-dl-"))
        cdp = await self.browser.new_browser_cdp_session()
        await cdp.send("Browser.setDownloadBehavior", {
            "behavior": "allow", "downloadPath": str(self._staging), "eventsEnabled": True,
        })
        self.detail = "installed Chrome over CDP"

    def _watch_downloads(self, page) -> None:
        """Playwright-launched browsers deliver downloads as events; a CDP-attached
        Chrome writes them straight into DOWNLOAD_DIR."""
        if self.mode == "real":
            return

        def on_download(download) -> None:
            task = asyncio.ensure_future(_save_download(download))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

        page.on("download", on_download)

    def _watch_pages(self) -> None:
        def on_page(page) -> None:
            self.opened_pages.append(page)
            self._watch_downloads(page)

        self.context.on("page", on_page)
        for page in self.context.pages:
            self._watch_downloads(page)

    async def new_downloads(self, wait: float = 8.0) -> list[Path]:
        """Files that appeared in DOWNLOAD_DIR since the last call. Waits while
        one is still being written."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait
        if self._tasks:
            await asyncio.wait(set(self._tasks), timeout=wait)
        await self._collect_staged(deadline)
        while True:
            fresh = _downloads_present() - self._known_downloads
            partial = [n for n in fresh if n.endswith(_PARTIAL)]
            if not partial or loop.time() >= deadline:
                break
            await asyncio.sleep(0.4)
        done = sorted(n for n in fresh if not n.endswith(_PARTIAL))
        self._known_downloads |= set(done)
        return [DOWNLOAD_DIR / n for n in done]

    async def _collect_staged(self, deadline: float) -> None:
        """Move finished files from the staging dir into DOWNLOAD_DIR."""
        if self._staging is None:
            return
        loop = asyncio.get_running_loop()
        while True:
            names = [p.name for p in self._staging.iterdir()]
            if not any(n.endswith(_PARTIAL) for n in names) or loop.time() >= deadline:
                break
            await asyncio.sleep(0.4)
        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        for name in names:
            if not name.endswith(_PARTIAL):
                shutil.move(str(self._staging / name), _unique(DOWNLOAD_DIR / name))

    async def close(self) -> None:
        if self.mode == "real" and self.browser is not None:
            with contextlib.suppress(Exception):
                await (await self.browser.new_browser_cdp_session()).send("Browser.close")
        for closer in (self.context, self.browser):
            if closer is not None:
                with contextlib.suppress(Exception):
                    await closer.close()
        if self.playwright is not None:
            with contextlib.suppress(Exception):
                await self.playwright.stop()
        for proc in self._procs:
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), 5)
                except TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        proc.kill()
        for leftover in (self._tmpdir, self._staging):
            if leftover is not None:
                # Chrome can still be flushing files as it exits.
                for _ in range(4):
                    shutil.rmtree(leftover, ignore_errors=True)
                    if not leftover.exists():
                        break
                    await asyncio.sleep(0.4)


async def _save_download(download) -> None:
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    name = Path(download.suggested_filename).name or "download"
    await download.save_as(_unique(DOWNLOAD_DIR / name))


async def open_session(mode: str, profile: str | None) -> Session:
    from playwright.async_api import async_playwright

    session = Session(mode, profile)
    try:
        session.playwright = await async_playwright().start()
        if mode == "real":
            await session._open_real()
        else:
            await session._open_playwright()
        session._watch_pages()
    except BaseException:
        await session.close()
        raise
    return session
