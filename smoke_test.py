"""Fast sanity checks — no API key, no network, no running workers needed.

    python smoke_test.py

This is not a test suite. It checks the wiring that breaks silently, which in
this repo is a different set of things from ResearchMesh:

  1. every module imports, and every file byte-compiles
  2. tool names are namespaced, unique, and legal for the Anthropic API
  3. the namespacing round-trips — a call on `worker__tool` reaches the right
     worker as plain `tool`
  4. a dead worker is skipped rather than taking the fleet down
  5. execution fans out across workers but stays serial within one
  6. every tool_use block gets exactly one tool_result, in the original order

(2) is the whole reason this repo exists. Two ResearchMesh workers both expose a
tool called `delegate`, and sending both to the API is a hard failure:

    400 invalid_request_error: tools: Tool names must be unique.

That is a real, verified response, not a defensive guess — and nothing else in
this codebase would catch a regression in it until a second worker was connected
and a real turn was attempted.

(5) matters because the fan-out is the feature. A refactor that quietly reverts
`execute_blocks` to a sequential loop would still pass every other check here
and still return correct answers — just three times slower, invisibly.

Only the four module-level dependencies are required, so this runs on a bare CI
box with no workers configured and no ANTHROPIC_API_KEY set.
"""

import asyncio
import re
import subprocess
import sys
from pathlib import Path

from mcp.types import CallToolResult, TextContent, Tool

ROOT = Path(__file__).resolve().parent
FAILURES: list[str] = []

# The API's own rule for a tool name, which is what check_namespacing enforces
# locally so you don't need a live request to find out you broke it.
TOOL_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name}{' — ' + detail if detail else ''}")
        FAILURES.append(name)


class Block:
    """Stand-in for an Anthropic tool_use content block."""

    def __init__(self, block_id: str, name: str, payload=None):
        self.id = block_id
        self.name = name
        self.input = payload or {}


class FakeWorker:
    """A worker that records what it was asked and how calls overlapped."""

    # Shared across instances, to observe cross-worker concurrency.
    live = 0
    peak = 0

    def __init__(self, tool_names, fail_listing=False):
        self._tools = [
            Tool(
                name=n,
                description=f"does {n}",
                input_schema={"type": "object", "properties": {}},
            )
            for n in tool_names
        ]
        self._fail_listing = fail_listing
        self.calls: list[str] = []
        self.own_peak = 0
        self._own_live = 0

    async def list_tools(self):
        if self._fail_listing:
            raise ConnectionError("worker is down")
        return self._tools

    async def call_tool(self, tool_name, tool_input):
        self.calls.append(tool_name)
        self._own_live += 1
        FakeWorker.live += 1
        self.own_peak = max(self.own_peak, self._own_live)
        FakeWorker.peak = max(FakeWorker.peak, FakeWorker.live)
        try:
            # Long enough that a sequential implementation cannot overlap and a
            # concurrent one always will.
            await asyncio.sleep(0.05)
            return CallToolResult(
                content=[TextContent(type="text", text=f"ran {tool_name}")]
            )
        finally:
            self._own_live -= 1
            FakeWorker.live -= 1


def check_compiles() -> None:
    print("byte-compile")
    files = sorted(ROOT.glob("*.py")) + sorted((ROOT / "core").glob("*.py"))
    result = subprocess.run(
        [sys.executable, "-m", "py_compile", *(str(f) for f in files)],
        capture_output=True,
        text=True,
        check=False,
    )
    check("all files compile", result.returncode == 0, result.stderr.strip()[:300])


def check_imports() -> None:
    print("imports")
    import importlib

    modules = [f"core.{p.stem}" for p in sorted((ROOT / "core").glob("*.py"))]
    modules = [m for m in modules if not m.endswith("__init__")]
    modules += ["main", "mcp_client"]
    for name in modules:
        try:
            importlib.import_module(name)
            check(name, True)
        except Exception as e:
            check(name, False, f"{type(e).__name__}: {e}")


def check_namespacing() -> None:
    """Two workers, same tool name — the case that 400s without namespacing."""
    print("namespacing")
    from core.tools import ToolManager

    workers = {
        "gpu-box": FakeWorker(["delegate"]),
        "win.box 2": FakeWorker(["delegate", "screenshot"]),
    }
    descriptions = {"gpu-box": "Headless CUDA machine, no display."}

    index = asyncio.run(ToolManager.build(workers, descriptions))
    names = [t["name"] for t in index.tool_defs]

    check("all three tools declared", len(names) == 3, f"got {names}")
    check(
        "no duplicate tool names",
        len(names) == len(set(names)),
        f"dupes in {names}",
    )
    check(
        "every name is API-legal",
        all(TOOL_NAME_RE.match(n) for n in names),
        f"illegal: {[n for n in names if not TOOL_NAME_RE.match(n)]}",
    )
    check(
        "illegal characters in a worker id are sanitised",
        any(n.startswith("win_box_2__") for n in names),
        f"got {names}",
    )

    # The namespacing must round-trip: the worker is never told it was renamed.
    gpu_tool = next(n for n in names if n.startswith("gpu-box__"))
    resolved = index.resolve(gpu_tool)
    check("resolve() finds an owner", resolved is not None)
    if resolved:
        client, bare = resolved
        check("resolves to the right worker", client is workers["gpu-box"])
        check("strips the prefix back off", bare == "delegate", f"got {bare!r}")

    # Identity: without this the two `delegate` tools are indistinguishable.
    gpu_def = next(t for t in index.tool_defs if t["name"] == gpu_tool)
    check(
        "description carries the worker tag",
        gpu_def["description"].startswith("[worker: gpu-box]"),
        gpu_def["description"][:60],
    )
    check(
        "description carries the config blurb",
        "Headless CUDA machine" in gpu_def["description"],
    )
    check(
        "worker's own description is preserved",
        "does delegate" in gpu_def["description"],
    )


def check_dead_worker_skipped() -> None:
    print("dead worker")
    from core.tools import ToolManager

    workers = {
        "alive": FakeWorker(["delegate"]),
        "dead": FakeWorker(["delegate"], fail_listing=True),
    }
    index = asyncio.run(ToolManager.build(workers, {}))
    names = [t["name"] for t in index.tool_defs]
    check("dead worker skipped, fleet survives", names == ["alive__delegate"], f"got {names}")


def check_fanout_and_results() -> None:
    """Groups run concurrently; a single worker's calls do not overlap."""
    print("fan-out and results")
    from core.tools import ToolManager

    workers = {
        "a": FakeWorker(["delegate"]),
        "b": FakeWorker(["delegate"]),
        "c": FakeWorker(["delegate"]),
    }
    index = asyncio.run(ToolManager.build(workers, {}))
    FakeWorker.live = FakeWorker.peak = 0

    # Two calls at worker `a` (must serialise), one each at `b` and `c`
    # (must overlap with `a` and with each other).
    blocks = [
        Block("id1", "a__delegate"),
        Block("id2", "b__delegate"),
        Block("id3", "a__delegate"),
        Block("id4", "c__delegate"),
    ]
    results = asyncio.run(ToolManager.execute_blocks(index, blocks))

    check("one result per block", len(results) == len(blocks), f"got {len(results)}")
    check(
        "results are in the original block order",
        [r["tool_use_id"] for r in results] == ["id1", "id2", "id3", "id4"],
        f"got {[r['tool_use_id'] for r in results]}",
    )
    check("no result is an error", not any(r["is_error"] for r in results))
    check(
        "workers were called with their own bare tool name",
        workers["a"].calls == ["delegate", "delegate"],
        f"got {workers['a'].calls}",
    )
    check(
        "different workers ran concurrently",
        FakeWorker.peak >= 3,
        f"peak concurrent workers was {FakeWorker.peak}, expected >= 3",
    )
    check(
        "one worker's calls did not overlap",
        workers["a"].own_peak == 1,
        f"worker 'a' had {workers['a'].own_peak} calls in flight at once",
    )

    # An unknown tool still owes the API a tool_result, or the whole session
    # breaks on the next request.
    orphan = asyncio.run(
        ToolManager.execute_blocks(index, [Block("id9", "nope__missing")])
    )
    check("unknown tool still returns a result", len(orphan) == 1)
    check("unknown tool result is flagged as an error", bool(orphan[0]["is_error"]))

    # max_parallel must actually bound concurrency.
    FakeWorker.live = FakeWorker.peak = 0
    asyncio.run(
        ToolManager.execute_blocks(
            index,
            [Block(f"p{i}", f"{w}__delegate") for i, w in enumerate("abc")],
            max_parallel=1,
        )
    )
    check(
        "max_parallel=1 serialises the whole fleet",
        FakeWorker.peak == 1,
        f"peak was {FakeWorker.peak}",
    )


def main() -> int:
    sys.path.insert(0, str(ROOT))
    for step in (
        check_compiles,
        check_imports,
        check_namespacing,
        check_dead_worker_skipped,
        check_fanout_and_results,
    ):
        step()
        print()

    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): {', '.join(FAILURES)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
