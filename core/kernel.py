"""Stateful Python: a real IPython kernel driven over jupyter_client.

State survives between calls (load a dataframe in one, query it in the next),
and pandas, sympy, matplotlib, duckdb, Pillow and pypdf come as plain imports
instead of separate tools. The kernel starts on first use and is reused for the
session, so `jupyter_client` is needed only if the tool is used. Its ZeroMQ
link carries all code and results, so it is encrypted with CurveZMQ where the
installed versions allow it (see `_start_manager` and
`CLAUDE_KERNEL_ENCRYPTION`).

Requires:  pip install 'jupyter_client>=8.9.1' 'ipykernel>=7'
"""

import asyncio
import json
import os
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
_ENCRYPTION_ENV = "CLAUDE_KERNEL_ENCRYPTION"

_manager = None
_client = None


def handles(name: str) -> bool:
    return name in _TOOL_NAMES


async def execute(name: str, tool_input: dict) -> str:
    if name != "python":
        return json.dumps({"error": f"unknown kernel tool {name!r}"})
    # jupyter_client's blocking client would stall the event loop.
    return await asyncio.to_thread(_run, tool_input)


def _encryption_policy() -> str:
    value = (os.environ.get(_ENCRYPTION_ENV) or "auto").strip().lower()
    if value not in ("auto", "required", "off"):
        print(f"[kernel] ignoring {_ENCRYPTION_ENV}={value!r}: want auto, required or off")
        return "auto"
    return value


def _start_encrypted():
    """A CurveZMQ-encrypted kernel, or None if this environment cannot provide
    one.

    By default the kernel's ZeroMQ link is plaintext on four loopback TCP
    ports. `transport_encryption` makes the manager generate a CurveZMQ keypair
    and pass it through the connection file (mode 600, no more exposed than the
    HMAC key already in it), so both ends use CURVE.

    "required", not "auto": "auto" falls back to plaintext silently when the
    kernelspec does not advertise `metadata.supported_encryption`; "required"
    turns that into a startup error.

    Needs jupyter_client >= 8.9.1 (8.9.0 breaks restart), an ipykernel whose
    kernelspec declares curve support, and a pyzmq built with libsodium. Older
    or partial installs raise here and take the fallbacks.
    """
    from jupyter_client import KernelManager

    manager = None
    try:
        manager = KernelManager(transport_encryption="required")
        manager.start_kernel()
        if manager.curve_publickey is None:  # provisioning quietly did nothing
            raise RuntimeError("manager provisioned no CurveZMQ keypair")
        return manager
    except Exception as e:
        print(f"[kernel] CurveZMQ transport encryption unavailable: {e}")
        if manager is not None:
            _quietly(manager.shutdown_kernel, now=True)
    return None


def _start_ipc():
    """An unencrypted kernel over IPC, or None. The second fallback.

    A user-only socket file in the Jupyter runtime dir is a smaller target than
    an open loopback port. jupyter_client provisions Curve for
    `transport="tcp"` only, so this cannot be combined with encryption.

    `ip` is set explicitly because the default for ipc is the relative prefix
    "kernel-ipc", which would put socket files in the cwd, leave them behind if
    the process is killed, and collide between instances.
    """
    from jupyter_client import KernelManager
    from jupyter_core.paths import jupyter_runtime_dir

    manager = None
    try:
        runtime_dir = jupyter_runtime_dir()
        os.makedirs(runtime_dir, exist_ok=True)
        prefix = os.path.join(runtime_dir, f"researchmesh-{os.getpid()}-ipc")
        # An AF_UNIX path is capped near 108 bytes and "-<port>" is appended.
        if len(prefix) > 100:
            raise OSError(f"socket path prefix too long: {prefix}")
        manager = KernelManager(transport="ipc", ip=prefix)
        manager.start_kernel()
        return manager
    except Exception as e:
        print(f"[kernel] IPC transport unavailable: {e}")
        if manager is not None:
            _quietly(manager.shutdown_kernel, now=True)
    return None


def _start_manager():
    """Start the kernel on the most protected transport this box supports.

    Encrypted TCP → IPC → plaintext TCP, each tier printing why it fell through.
    `CLAUDE_KERNEL_ENCRYPTION=required` stops at the first tier and fails the
    tool rather than running in the clear; `off` skips straight to IPC.
    """
    from jupyter_client import KernelManager

    policy = _encryption_policy()
    if policy != "off":
        manager = _start_encrypted()
        if manager is not None:
            return manager
        if policy == "required":
            raise RuntimeError(
                f"{_ENCRYPTION_ENV}=required and CurveZMQ is unavailable (see above) — "
                "`pip install -U 'jupyter_client>=8.9.1' 'ipykernel>=7'`, or unset it to "
                "fall back to an unencrypted local kernel"
            )

    manager = _start_ipc()
    if manager is not None:
        return manager

    manager = KernelManager()
    manager.start_kernel()
    return manager


def _new_client(manager):
    """`manager.client()`, with the CurveZMQ keypair re-supplied as bytes.

    Workaround for jupyter_client 8.9.1: `get_connection_info()` decodes the
    keypair to str, but the client's `curve_publickey`/`curve_secretkey` traits
    are `Bytes`, so the bare call raises a TraitError on an encrypted kernel.
    `client()` applies **kwargs last, so passing the manager's own bytes fixes
    it and stays correct once upstream does.
    """
    extra = {}
    if getattr(manager, "curve_publickey", None) is not None:
        extra["curve_publickey"] = manager.curve_publickey
        extra["curve_secretkey"] = manager.curve_secretkey
    return manager.client(**extra)


def _ensure_kernel() -> str | None:
    """Start the kernel if needed. Returns an error string, or None on success."""
    global _manager, _client
    if _client is not None:
        return None
    try:
        import jupyter_client  # noqa: F401
    except ImportError:
        return (
            "jupyter_client is not installed — `pip install jupyter_client "
            "ipykernel` to enable the stateful python tool"
        )
    try:
        _manager = _start_manager()
        _client = _new_client(_manager)
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
        _client = _new_client(_manager)
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


def _quietly(call, **kwargs):
    # Blanket except on purpose (ruff BLE001): this runs on the way out and
    # must not fail. stop_channels() ends in pyzmq's context.destroy(), and
    # zmq.ZMQError is not an OSError, so a narrower except would let it escape
    # as a traceback on an ordinary Ctrl-C. The print() makes the failure
    # visible instead of a silent `pass`.
    try:
        call(**kwargs)
    except Exception as e:
        print(f"[kernel] {getattr(call, '__name__', call)} failed (ignored): {e}")


def _shutdown_sync():
    global _manager, _client
    if _client is not None:
        _quietly(_client.stop_channels)
    if _manager is not None:
        # Also unlinks the IPC socket files, when that is the transport in use.
        _quietly(_manager.shutdown_kernel, now=True)
    _manager = _client = None


async def shutdown():
    """Stop the kernel. Safe to call even if it was never started."""
    if _manager is None and _client is None:
        return
    await asyncio.to_thread(_shutdown_sync)
