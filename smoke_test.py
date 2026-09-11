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
  7. the local half of the tool list is legal, cannot collide with a worker's
     namespaced name, and a turn mixing local and worker calls still returns one
     result per block in order
  8. `/dagent` withholds every local schema, and `/clear` plus the failure
     report can tell the two *persistent* 400s apart — an unanswered tool_use
     block and a conversation past the context window both leave every later
     turn failing identically, and they need different fixes

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
from typing import cast

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

    type = "tool_use"

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


def check_namespacing_edge_cases() -> None:
    """The two `_legalise()` responsibilities `check_namespacing` never triggers.

    That check's own two workers happen not to collide after sanitising and
    stay well under the 128-char API limit — so neither disambiguation nor
    hash-truncation, both explicitly named in CLAUDE.md as reasons this
    function exists, is exercised anywhere else. A regression in either would
    pass every other check in this file.
    """
    print("namespacing edge cases")
    from core.tools import ToolManager

    # "gpu box" and "gpu.box" both sanitise to "gpu_box" — space and period
    # are both illegal characters substituted with the same "_". (README's own
    # "gpu box and gpu-box would collide" example does NOT actually collide —
    # hyphen is already API-legal per _ILLEGAL's own pattern and is never
    # substituted at all; verified live, flagged as a separate doc fix.)
    # Without disambiguation, one worker's tool would silently shadow the
    # other's.
    colliding = {
        "gpu box": FakeWorker(["delegate"]),
        "gpu.box": FakeWorker(["delegate"]),
    }
    index = asyncio.run(ToolManager.build(colliding, {}))
    names = [t["name"] for t in index.tool_defs]
    check(
        "colliding sanitised names still end up unique",
        len(names) == len(set(names)) == 2,
        f"got {names}",
    )
    check(
        "both colliding workers are still independently resolvable",
        all(index.resolve(n) is not None for n in names),
        f"got {names}",
    )

    # A worker id long enough that "id__delegate" exceeds the 128-char API
    # limit forces the hash-truncation path in `_legalise()`, not just the
    # character-substitution one `check_namespacing` already covers.
    long_id = "x" * 150
    long_index = asyncio.run(ToolManager.build({long_id: FakeWorker(["delegate"])}, {}))
    long_names = [t["name"] for t in long_index.tool_defs]
    check(
        "a very long worker name is truncated, not silently over the API limit",
        all(len(n) <= 128 for n in long_names),
        f"lengths: {[len(n) for n in long_names]}",
    )
    check(
        "the truncated name is still API-legal",
        all(TOOL_NAME_RE.match(n) for n in long_names),
        f"got {long_names}",
    )

    # Truncation must be deterministic — a name that changed between requests
    # would silently cost the prompt-cache hit every turn (CLAUDE.md's own
    # stated reason `_digest()` hashes rather than counts).
    long_index_2 = asyncio.run(
        ToolManager.build({long_id: FakeWorker(["delegate"])}, {})
    )
    long_names_2 = [t["name"] for t in long_index_2.tool_defs]
    check(
        "truncation is deterministic across separate builds",
        long_names == long_names_2,
        f"{long_names} != {long_names_2}",
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


def check_local_tools() -> None:
    """The local half of the tool list, and its two ways of going wrong.

    Local tools are declared unprefixed alongside the namespaced worker tools,
    in one `tools` array the API requires to be unique across *both* halves. And
    once both halves exist, a turn can mix them — which is the only place in the
    codebase where tool_results are assembled from two different executors.
    """
    print("local tools")
    from core import local_tools
    from core.chat import Chat
    from core.claude import BETAS, Claude
    from core.tools import ToolManager

    names = [t["name"] for t in local_tools.TOOLS]
    check("local tools are declared", len(names) > 0, f"{len(names)} found")
    check("no duplicate local names", len(set(names)) == len(names))
    check(
        "every local name is API-legal",
        all(TOOL_NAME_RE.match(n) for n in names),
        str([n for n in names if not TOOL_NAME_RE.match(n)]),
    )
    check(
        "computer's beta flag is declared",
        any("computer" in b for b in BETAS),
        f"BETAS={BETAS}",
    )

    # A local name must never be reachable as a worker's namespaced name, or the
    # request 400s on a duplicate and takes every tool down with it.
    worker = FakeWorker(["delegate"])
    index = asyncio.run(
        ToolManager.build(
            {"w": worker}, {"w": "a worker"}, reserved=set(names)
        )
    )
    declared = [t["name"] for t in index.tool_defs]
    check(
        "worker names never collide with local ones",
        not (set(declared) & set(names)),
        str(set(declared) & set(names)),
    )

    # A mixed turn: local, worker, local, unknown. Every block owes exactly one
    # result, and the order must survive being split across two executors.
    # `_run_tool_uses` never touches claude_service, so a cast keeps the check
    # focused on routing rather than dragging a live API client in.
    chat = Chat(
        claude_service=cast(Claude, None),
        clients={"w": worker},
        descriptions={},
    )
    # names[0] is `bash` with no `command` — it returns an error string without
    # running anything, which keeps this check as network- and side-effect-free
    # as the rest of the file.
    blocks = [
        Block("b1", names[0]),
        Block("b2", "w__delegate"),
        Block("b3", names[0]),
        Block("b4", "totally__unknown"),
    ]

    class FakeMessage:
        content = blocks

    results = asyncio.run(chat._run_tool_uses(FakeMessage(), index))
    check("one result per block in a mixed turn", len(results) == len(blocks))
    check(
        "mixed results keep the original block order",
        [r["tool_use_id"] for r in results] == [b.id for b in blocks],
        str([r["tool_use_id"] for r in results]),
    )
    check(
        "the worker block reached the worker",
        worker.calls == ["delegate"],
        str(worker.calls),
    )


def check_dagent_and_workers() -> None:
    """`/dagent` must withhold the local tools, not merely discourage them.

    The whole point is that it is mechanical: a local `bash` is faster and more
    directly matched to any concrete command than a `delegate` that takes
    minutes, so an instruction is a preference the model can talk itself out of
    and an absent schema is not. If a refactor ever reverts this to a prompt
    tweak, every other check here still passes and the failure is invisible —
    the model just quietly does a worker's job on the wrong machine.
    """
    print("/dagent and /workers")
    from core import local_tools
    from core.chat import Chat
    from core.claude import Claude
    from core.tools import ToolManager

    local_names = {t["name"] for t in local_tools.TOOLS}
    workers = {"alpha": FakeWorker(["delegate"]), "beta": FakeWorker(["delegate"])}
    descriptions = {"alpha": "the first box", "beta": "the second box"}
    chat = Chat(
        claude_service=cast(Claude, None),
        clients=workers,
        descriptions=descriptions,
    )

    index = asyncio.run(
        ToolManager.build(workers, descriptions, reserved=local_names)
    )
    check("both workers are up", index.worker_ids() == ["alpha", "beta"])
    check(
        "defs_for narrows to one worker",
        [t["name"] for t in index.defs_for("beta")] == ["beta__delegate"],
        str([t["name"] for t in index.defs_for("beta")]),
    )
    check(
        "defs_for is empty for a worker that is not up",
        index.defs_for("gone") == [],
    )

    listing = asyncio.run(chat.workers_listing())
    check("listing names every worker up", all(w in listing for w in workers))
    check("listing carries the routing blurb", "the second box" in listing)

    # `/dagent` parsing: a leading worker name is a pin, anything else is task
    # text — a task that merely starts with a word must not lose it.
    check(
        "a leading worker name is peeled off",
        chat.split_worker("beta render the scene") == ("beta", "render the scene"),
    )
    check(
        "an unknown leading word stays in the task",
        chat.split_worker("render the scene") == (None, "render the scene"),
    )
    check(
        "a bare worker name is not a task",
        chat.split_worker("beta") == (None, "beta"),
    )

    # The guarantee itself, checked on the schemas that would actually be sent.
    everything = [t["name"] for t in local_tools.TOOLS + index.tool_defs]
    check("a normal turn offers both halves", local_names <= set(everything))
    remote = [t["name"] for t in index.tool_defs]
    check(
        "a /dagent turn offers no local tool",
        not (local_names & set(remote)),
        str(local_names & set(remote)),
    )
    check("a /dagent turn still offers the workers", len(remote) == 2)
    pinned = [t["name"] for t in index.defs_for("alpha")]
    check(
        "a pinned /dagent turn offers only that worker",
        pinned == ["alpha__delegate"],
        str(pinned),
    )


def check_clear_and_diagnostics() -> None:
    """`/clear`, and telling the two persistent 400s apart.

    Both leave the router failing every turn with no way back, and from the
    outside they look the same. The orphan detector is what separates them, so
    it is checked against a history that is deliberately poisoned — the
    condition `_resolve_pending_tool_uses` exists to prevent, constructed here
    on purpose because a passing router never produces one.
    """
    print("/clear and diagnostics")
    from core.chat import Chat, _approx_size, _orphaned_tool_uses
    from core.claude import Claude

    chat = Chat(claude_service=cast(Claude, None), clients={}, descriptions={})

    # A healthy exchange: tool_use answered by a matching tool_result.
    healthy = [
        {"role": "user", "content": "do a thing"},
        {"role": "assistant", "content": [Block("t1", "bash")]},
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "done"}
            ],
        },
    ]
    check("a healthy history has no orphans", _orphaned_tool_uses(healthy) == [])

    # The poisoned case: a tool_use nothing ever answered.
    poisoned = healthy + [
        {"role": "assistant", "content": [Block("t2", "gpu__delegate")]},
        {"role": "user", "content": "and another thing"},
    ]
    check(
        "an unanswered tool_use is detected",
        _orphaned_tool_uses(poisoned) == ["t2"],
        str(_orphaned_tool_uses(poisoned)),
    )

    count, chars = _approx_size(poisoned)
    check("size report counts every message", count == len(poisoned), str(count))
    check("size report counts characters", chars > 0, str(chars))

    # /clear must actually empty it, and say what it threw away.
    chat.messages = cast(list, list(poisoned))
    report = chat.clear()
    check("clear() empties the conversation", chat.messages == [])
    check("clear() reports the message count", "5 messages" in report, report)
    check(
        "clear() names the unanswered block as the cause",
        "unanswered tool_use" in report,
        report,
    )
    check("clear() is safe on an empty conversation", "0 messages" in chat.clear())

    # The diagnostic must not raise on either failure shape — it runs on an
    # already-failing path, so an exception here would mask the real error.
    for label, err in (
        ("overflow", Exception("prompt is too long: 1200000 tokens > 1000000")),
        ("orphan", Exception("tool_use ids were found without tool_result")),
    ):
        chat.messages = cast(list, list(poisoned))
        try:
            chat._report_api_failure(err)
            ok = True
        except Exception as e:
            ok, label = False, f"{label}: {e}"
        check(f"failure report survives a {label} error", ok)


def main() -> int:
    sys.path.insert(0, str(ROOT))
    for step in (
        check_compiles,
        check_imports,
        check_namespacing,
        check_namespacing_edge_cases,
        check_dead_worker_skipped,
        check_fanout_and_results,
        check_local_tools,
        check_dagent_and_workers,
        check_clear_and_diagnostics,
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
