"""A private asyncio loop in a daemon thread, for code that owns a D-Bus connection.

The tool modules run their blocking work in worker threads, so a D-Bus
connection (dbus-next is asyncio-only) lives on its own loop and is reached
through `run`.
"""

import asyncio
import threading


class Loop:
    def __init__(self, name: str = "dbus") -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self.loop.run_forever, daemon=True, name=name)
        self._thread.start()

    def run(self, coro, timeout: float):
        """Run a coroutine on the loop and wait for its result."""
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(5)
