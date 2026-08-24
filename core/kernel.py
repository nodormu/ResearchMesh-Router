"""Stateful Python — a real IPython kernel driven over jupyter_client.

This is the one thing the bash tool structurally cannot do: state survives
between calls. Load a dataframe in one call, query it in the next. It also
absorbs a whole shelf of would-be tools as plain imports (pandas, sympy,
matplotlib, duckdb, Pillow, pypdf) instead of spending a tool slot on each.

The kernel is launched lazily on first use and reused for the rest of the
session, so `jupyter_client` only has to be installed if the tool is used.

Requires:  pip install jupyter_client ipykernel
"""

import asyncio
import json
import queue
import re
import time

from core.output import clip

# IPython colours its tracebacks; the escape codes are pure context bloat here.
_ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

TOOLS = [
    {
        "name": "python",
        "description": (
            "Run Python in a persistent IPython kernel. Variables, imports, and "
            "open files PERSIST between calls, unlike the bash tool which starts "
            "a fresh subprocess every time — so load data once and query it over "
            "several calls. Use this for computation, data wrangling, and plotting "
            "(save figures to disk and report the path; images are not returned "
            "inline). Returns JSON: output, error traceback if it raised, and the "
            "repr of the last expression."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Python source to execute in the kernel.",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Seconds to wait for completion (default 60).",
                },
                "restart": {
                    "type": "boolean",
                    "description": (
                        "Restart the kernel first, discarding all state. Use when "
                        "the kernel is wedged or you want a clean namespace."
                    ),
                },
            },
            "required": ["code"],
        },
    }
]

_TOOL_NAMES = {t["name"] for t in TOOLS}
_MAX_OUTPUT = 12000
_DEFAULT_TIMEOUT = 60

_manager = None
_client = None


def handles(name: str) -> bool:
    return name in _TOOL_NAMES


async def execute(name: str, tool_input: dict) -> str:
    if name != "python":
        return json.dumps({"error": f"unknown kernel tool {name!r}"})
    # jupyter_client's blocking client would stall the event loop.
    return await asyncio.to_thread(_run, tool_input)


def _ensure_kernel() -> str | None:
    """Start the kernel if needed. Returns an error string, or None on success."""
    global _manager, _client
    if _client is not None:
        return None
    try:
        from jupyter_client import KernelManager
    except ImportError:
        return (
            "jupyter_client is not installed — `pip install jupyter_client "
            "ipykernel` to enable the stateful python tool"
        )
    try:
        _manager = KernelManager()
        _manager.start_kernel()
        _client = _manager.client()
        _client.start_channels()
        _client.wait_for_ready(timeout=60)
    except Exception as e:
        _shutdown_sync()
        return f"could not start the IPython kernel: {e}"
    return None


def _restart() -> str | None:
    global _client
    if _manager is None:
        return _ensure_kernel()
    try:
        _manager.restart_kernel(now=True)
        _client = _manager.client()
        _client.start_channels()
        _client.wait_for_ready(timeout=60)
    except Exception as e:
        return f"could not restart the kernel: {e}"
    return None


def _run(tool_input: dict) -> str:
    if tool_input.get("restart"):
        error = _restart()
        if error:
            return json.dumps({"error": error})
    else:
        error = _ensure_kernel()
        if error:
            return json.dumps({"error": error})

    code = tool_input.get("code", "")
    if not code.strip():
        return json.dumps({"error": "no code provided"})

    # Both are set by _ensure_kernel/_restart above, which return an error
    # string if they couldn't — so this is unreachable in practice. It is
    # spelled out rather than assumed because every use below dereferences
    # them, and a silent None here would be an AttributeError mid-execution.
    if _client is None or _manager is None:
        return json.dumps({"error": "kernel is not running"})

    timeout = int(tool_input.get("timeout") or _DEFAULT_TIMEOUT)
    msg_id = _client.execute(code)
    deadline = time.monotonic() + timeout

    chunks: list[str] = []
    result = None
    traceback_text = None
    images = 0

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _manager.interrupt_kernel()
            chunks.append(f"\n[interrupted: exceeded {timeout}s]")
            break
        try:
            msg = _client.get_iopub_msg(timeout=remaining)
        except queue.Empty:
            continue
        except Exception as e:  # channel died mid-execution
            traceback_text = f"kernel channel error: {e}"
            break

        if msg.get("parent_header", {}).get("msg_id") != msg_id:
            continue  # output from an earlier, timed-out call

        msg_type = msg["msg_type"]
        content = msg["content"]

        if msg_type == "stream":
            chunks.append(content.get("text", ""))
        elif msg_type in ("execute_result", "display_data"):
            data = content.get("data", {})
            if "text/plain" in data:
                text = data["text/plain"]
                if msg_type == "execute_result":
                    result = text
                else:
                    chunks.append(text)
            if any(k.startswith("image/") for k in data):
                images += 1
        elif msg_type == "error":
            traceback_text = _ANSI.sub("", "\n".join(content.get("traceback", [])))
        elif msg_type == "status" and content.get("execution_state") == "idle":
            break

    payload = {
        "output": clip("".join(chunks), _MAX_OUTPUT),
        "result": result,
        "error": traceback_text,
    }
    if images:
        payload["note"] = (
            f"{images} inline image(s) produced but not returned — save figures "
            "to a file and report the path instead"
        )
    return json.dumps(payload)


def _shutdown_sync():
    # Both excepts are deliberately blanket (ruff BLE001) rather than narrowed.
    # These run on the way out and must not be able to fail: stop_channels()
    # ends in pyzmq's context.destroy(), and zmq.ZMQError derives from
    # Exception, *not* OSError — so `except (RuntimeError, OSError)` lets it
    # escape, out through local_tools.shutdown() and the AsyncExitStack, into a
    # traceback on an ordinary Ctrl-C. The S110 finding these replaced was about
    # the silent `pass`, not the breadth, so the print() is the actual fix.
    global _manager, _client
    if _client is not None:
        try:
            _client.stop_channels()
        except Exception as e:
            print(f"[kernel] stop_channels failed (ignored): {e}")
    if _manager is not None:
        try:
            _manager.shutdown_kernel(now=True)
        except Exception as e:
            print(f"[kernel] shutdown_kernel failed (ignored): {e}")
    _manager = _client = None


async def shutdown():
    """Stop the kernel. Safe to call even if it was never started."""
    if _manager is None and _client is None:
        return
    await asyncio.to_thread(_shutdown_sync)
