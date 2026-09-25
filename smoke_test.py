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
from typing import ClassVar, cast

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

    def __init__(
        self,
        tool_names,
        fail_listing=False,
        call_tool_result: CallToolResult | None = None,
        call_tool_error: Exception | None = None,
    ):
        self._tools = [
            Tool(
                name=n,
                description=f"does {n}",
                input_schema={"type": "object", "properties": {}},
            )
            for n in tool_names
        ]
        self._fail_listing = fail_listing
        # Both optional and both None by default, so every EXISTING caller
        # (check_dagent_and_workers, check_fanout_and_results, ...) keeps
        # getting the original fixed "ran {tool_name}" response, unchanged.
        # Set one of these to script a specific `model`-tool-style response
        # (e.g. a numbered list, or is_error=True) or a transport failure,
        # without needing a real MCP round trip — see
        # check_model_worker_dispatch().
        self._call_tool_result = call_tool_result
        self._call_tool_error = call_tool_error
        self.calls: list[str] = []
        self.call_args: list[dict] = []
        self.own_peak = 0
        self._own_live = 0

    async def list_tools(self):
        if self._fail_listing:
            raise ConnectionError("worker is down")
        return self._tools

    async def call_tool(self, tool_name, tool_input):
        self.calls.append(tool_name)
        self.call_args.append(tool_input)
        self._own_live += 1
        FakeWorker.live += 1
        self.own_peak = max(self.own_peak, self._own_live)
        FakeWorker.peak = max(FakeWorker.peak, FakeWorker.live)
        try:
            # Long enough that a sequential implementation cannot overlap and a
            # concurrent one always will.
            await asyncio.sleep(0.05)
            if self._call_tool_error is not None:
                raise self._call_tool_error
            if self._call_tool_result is not None:
                return self._call_tool_result
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


def check_docs_match_code() -> None:
    """The other three ResearchMesh forks (Linux, Mac, Windows) all share a
    near-identical version of this check: a regex hunts README.md/CLAUDE.md
    for a stated tool COUNT ("N local tools", etc.) and asserts it equals
    `len(local_tools.TOOLS)`. That exact check does NOT fit this fork,
    confirmed by reading both docs rather than assumed — porting it
    verbatim would either silently pass on a coincidence or permanently
    fail on a doc that was never wrong in the first place:

      - README.md DOES state a plain count ("**23 local tools**", "the 23
        local tools alone") consistently, so that half of the borrowed
        check is reused unchanged below.
      - CLAUDE.md deliberately NEVER states a raw tool-count number
        anywhere. It documents the local half of the tool list via a
        "Where it came from" file-provenance table instead — which
        modules were copied verbatim from ResearchMesh vs. diverged — a
        real, intentional style choice suited to this fork's specific
        job (tracking inheritance), not an oversight. A borrowed numeric
        check would have nothing to find here and either false-fail
        ("no tool-count phrasing found") forever or need to be silently
        skipped, neither of which actually checks anything.

    So CLAUDE.md gets a DIFFERENT check instead, one that matches what it
    actually promises: every local-tool-backing module file that
    `core/local_tools.py` really imports should be mentioned SOMEWHERE in
    CLAUDE.md (its provenance table or otherwise) — so a new tool module
    added to MODULES without a single word added to CLAUDE.md fails
    loudly, which is the same "silent doc drift" this whole check family
    exists to catch, just aimed at the promise THIS file's docs actually
    make rather than a borrowed one they don't.
    """
    print("docs vs code")
    import inspect
    import os

    from core import local_tools

    actual = len(local_tools.TOOLS)
    pattern = re.compile(
        r"(\d+) local tools?"
        r"|(\d+) local \+"
        r"|tools?,? not (\d+)\b"
    )
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    claimed = {int(g) for m in pattern.finditer(readme) for g in m.groups() if g}
    if not claimed:
        check("README.md: states a tool count", False, "no tool-count phrasing found")
    else:
        check(
            f"README.md: claims {sorted(claimed)} == actual {actual}",
            claimed == {actual},
        )

    claude_md = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    missing = []
    for module in local_tools.MODULES:
        src = inspect.getsourcefile(module)
        name = os.path.basename(src) if src else module.__name__
        if name not in claude_md:
            missing.append(name)
    check(
        "CLAUDE.md: every local-tool module file is mentioned somewhere",
        not missing, str(missing),
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


def check_run_loop_tool_use_lifecycle() -> None:
    """Drive the real, unmodified `Chat.run()` against a scripted fake API:
    the cutoff-duplicate bug, self-healing an already-poisoned history
    (reproduces the actual ResearchMesh production error, same core/chat.py
    lineage before this port), pause_turn replace-not-append, a mandatory
    mixed-call follow-up, grace-budget-exhausted surgical excision (never a
    turn/conversation wipe), and a normal multi-round regression guard.

    Ported from ResearchMesh's smoke_test.py (same-named function) alongside
    the core/chat.py fix itself (commit 672aae1 there). The only structural
    difference from the original: this router calls `ToolManager.build`
    (returning a `ToolIndex`) rather than ResearchMesh's `get_all_tools`, so
    the fake here returns a minimal fake index instead of a bare list —
    everything else (the six scenarios, the assertions) is the same
    reproduction of the same bug class, since this router is reachable by it
    too (it also declares web_search/web_fetch as real Anthropic server
    tools — see core/claude_learned_schemas.py).

    See ResearchMesh's researchmesh_client_dev_log.md for the full incident
    history behind each scenario — not duplicated here since it's the same
    root cause, ported.
    """
    print("run() loop: tool_use lifecycle (cutoff, self-heal, pause_turn, mixed calls)")
    import core.chat as chat_mod
    from core.chat import Chat, _duplicate_tool_result_ids, _orphaned_tool_uses

    class FakeBlock:
        def __init__(self, type, **kw):
            self.type = type
            for k, v in kw.items():
                setattr(self, k, v)

    class FakeResponse:
        def __init__(self, stop_reason, content):
            self.stop_reason = stop_reason
            self.content = content
            self.usage = type(
                "U", (), {"input_tokens": 1, "output_tokens": 1}
            )()

    class FakeClaudeService:
        def __init__(self, script):
            self._script = list(script)
            self.calls = 0

        def add_user_message(self, messages, message):
            messages.append(
                {
                    "role": "user",
                    "content": message.content
                    if hasattr(message, "content")
                    else message,
                }
            )

        def add_assistant_message(self, messages, message):
            messages.append(
                {
                    "role": "assistant",
                    "content": message.content
                    if hasattr(message, "content")
                    else message,
                }
            )

        def text_from_message(self, message):
            return "\n".join(
                b.text for b in message.content if b.type == "text"
            )

        def chat(
            self, messages, system=None, stop_sequences=None, tools=None,
            thinking=False,
        ):
            self.calls += 1
            item = self._script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

    class FakeIndex:
        """Minimal stand-in for core.tools.ToolIndex — only what run()'s
        non-remote_only path actually touches: `.tool_defs` (concatenated
        onto local_tools.TOOLS) and `.summary()` (a one-line announce
        print). Every scenario here runs the ordinary local-tools branch,
        so `.defs_for`/`.worker_ids` (the remote_only path) are never
        called and aren't stubbed."""
        tool_defs: ClassVar[list] = []

        def summary(self):
            return "0 workers"

    async def fake_execute(name, input):
        return "ok"

    async def fake_build(cls, clients, descriptions=None, reserved=None):
        return FakeIndex()

    orig_execute = chat_mod.local_tools.execute
    orig_build = chat_mod.ToolManager.build
    orig_max_iter = chat_mod.MAX_TOOL_ITERATIONS
    orig_extra_limit = chat_mod.EXTRA_CONTINUATION_LIMIT
    chat_mod.local_tools.execute = fake_execute
    chat_mod.ToolManager.build = classmethod(fake_build)  # type: ignore[assignment]

    try:
        # --- 1: cutoff right after an ordinary tool_use round trip
        chat_mod.MAX_TOOL_ITERATIONS = 1
        fake1 = FakeClaudeService([
            FakeResponse(
                "tool_use", [FakeBlock("tool_use", id="t1", name="bash", input={})]
            )
        ])
        c1 = Chat(claude_service=fake1, clients={})  # type: ignore[arg-type]
        result1 = asyncio.run(c1.run("do a thing"))
        check("cutoff: exactly one chat() call", fake1.calls == 1, str(fake1.calls))
        check(
            "cutoff: no duplicate tool_result",
            not _duplicate_tool_result_ids(c1.messages),
        )
        check(
            "cutoff: no orphaned tool_use", not _orphaned_tool_uses(c1.messages)
        )
        check(
            "cutoff: reports the iteration limit",
            "exceeded tool-iteration limit" in result1,
            result1,
        )

        # --- 2: self-heal an already-poisoned history (exact prod repro,
        # same id ResearchMesh's own report used — carried over verbatim
        # since it's what makes this a repro rather than a synthetic case)
        chat_mod.MAX_TOOL_ITERATIONS = 75
        dup_id = "toolu_01Lz4DQdjjho9bntBh7LtYWJ"
        fake2 = FakeClaudeService([
            Exception(
                "each tool_use must have a single result. Found multiple "
                f"`tool_result` blocks with id: {dup_id}"
            ),
            FakeResponse("end_turn", [FakeBlock("text", text="all better now")]),
        ])
        c2 = Chat(claude_service=fake2, clients={})  # type: ignore[arg-type]
        c2.messages = [
            {"role": "user", "content": "earlier turn one"},
            {"role": "assistant", "content": "answer one"},
            {
                "role": "assistant",
                "content": [FakeBlock("tool_use", id=dup_id, name="bash", input={})],  # type: ignore[list-item]
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": dup_id, "content": "the REAL result"},
                    {
                        "type": "tool_result",
                        "tool_use_id": dup_id,
                        "content": "[stopped: exceeded tool-iteration limit]",
                        "is_error": True,
                    },
                ],
            },
        ]
        earlier_turns_before = [dict(m) for m in c2.messages[:2]]
        result2 = asyncio.run(c2.run("please continue"))
        check(
            "self-heal: one failed call + one successful retry",
            fake2.calls == 2, str(fake2.calls),
        )
        check(
            "self-heal: duplicate removed",
            not _duplicate_tool_result_ids(c2.messages),
        )
        check("self-heal: no orphan left behind", not _orphaned_tool_uses(c2.messages))
        check(
            "self-heal: earlier turns preserved exactly",
            c2.messages[:2] == earlier_turns_before,
        )
        check(
            "self-heal: retried request's real answer returned",
            "all better now" in result2, result2,
        )

        # --- 2b: self-heal a ZERO-result orphan (a distinct production
        # error shape -- a tool_use with NO result at all, sitting as the
        # very last message, vs. scenario 2's duplicate-result shape)
        chat_mod.MAX_TOOL_ITERATIONS = 75
        orphan_id = "toolu_01GegL1vVQgSDzMC2d6WfAsJ"
        fake2b = FakeClaudeService([
            Exception(
                "messages.188: `tool_use` ids were found without "
                f"`tool_result` blocks immediately after: {orphan_id}."
            ),
            FakeResponse("end_turn", [FakeBlock("text", text="picking up again")]),
        ])
        c2b = Chat(claude_service=fake2b, clients={})  # type: ignore[arg-type]
        c2b.messages = [
            {"role": "user", "content": "earlier turn"},
            {"role": "assistant", "content": "earlier answer"},
            {
                "role": "assistant",
                "content": [FakeBlock("tool_use", id=orphan_id, name="bash", input={})],  # type: ignore[list-item]
            },
        ]
        before_2b = [dict(m) for m in c2b.messages[:2]]
        result2b = asyncio.run(c2b.run("continue please"))
        check(
            "zero-orphan: one failed call + one successful retry",
            fake2b.calls == 2, str(fake2b.calls),
        )
        check("zero-orphan: no orphan left behind", not _orphaned_tool_uses(c2b.messages))
        check(
            "zero-orphan: earlier turns preserved exactly",
            c2b.messages[:2] == before_2b,
        )
        check(
            "zero-orphan: retried request's real answer returned",
            "picking up again" in result2b, result2b,
        )

        # --- 3: pause_turn continuations REPLACE, never append
        chat_mod.MAX_TOOL_ITERATIONS = 75
        server_block = FakeBlock("server_tool_use", id="s1", name="web_search", input={})
        result_block = FakeBlock("web_search_tool_result", tool_use_id="s1", content=[])
        fake3 = FakeClaudeService([
            FakeResponse("pause_turn", [server_block]),
            FakeResponse("pause_turn", [server_block, FakeBlock("text", text="still going")]),
            FakeResponse(
                "end_turn",
                [server_block, result_block, FakeBlock("text", text="search done")],
            ),
        ])
        c3 = Chat(claude_service=fake3, clients={})  # type: ignore[arg-type]
        result3 = asyncio.run(c3.run("search for something"))
        assistant_msgs3 = [m for m in c3.messages if m["role"] == "assistant"]
        check(
            "pause_turn: exactly one assistant message for the whole turn",
            len(assistant_msgs3) == 1, str(len(assistant_msgs3)),
        )
        roles3 = [m["role"] for m in c3.messages]
        no_adjacent_assistant = all(
            not (roles3[i] == "assistant" == roles3[i + 1])
            for i in range(len(roles3) - 1)
        )
        check(
            "pause_turn: no two consecutive assistant messages",
            no_adjacent_assistant, str(roles3),
        )
        check("pause_turn: real final answer returned", "search done" in result3, result3)

        # --- 4: mixed client+server tool_use forces the mandatory follow-up
        chat_mod.MAX_TOOL_ITERATIONS = 1
        server_block4 = FakeBlock("server_tool_use", id="s1", name="web_search", input={})
        client_block4 = FakeBlock("tool_use", id="c1", name="bash", input={})
        fake4 = FakeClaudeService([
            FakeResponse("tool_use", [server_block4, client_block4]),
            FakeResponse(
                "end_turn",
                [
                    server_block4,
                    FakeBlock("web_search_tool_result", tool_use_id="s1", content=[]),
                    FakeBlock("text", text="all resolved"),
                ],
            ),
        ])
        c4 = Chat(claude_service=fake4, clients={})  # type: ignore[arg-type]
        result4 = asyncio.run(c4.run("mixed call"))
        check(
            "mixed call: mandatory follow-up made despite budget=1",
            fake4.calls == 2, str(fake4.calls),
        )
        check("mixed call: no orphan left behind", not _orphaned_tool_uses(c4.messages))
        check("mixed call: real final answer returned", "all resolved" in result4, result4)

        # --- 5: grace budget also exhausted -> surgical excise only
        chat_mod.MAX_TOOL_ITERATIONS = 1
        chat_mod.EXTRA_CONTINUATION_LIMIT = 1
        server_block5 = FakeBlock("server_tool_use", id="s1", name="web_search", input={})
        fake5 = FakeClaudeService([
            FakeResponse("pause_turn", [FakeBlock("text", text="searching"), server_block5]),
            FakeResponse("pause_turn", [FakeBlock("text", text="still searching"), server_block5]),
        ])
        c5 = Chat(claude_service=fake5, clients={})  # type: ignore[arg-type]
        c5.messages = [
            {"role": "user", "content": "earlier turn"},
            {"role": "assistant", "content": "earlier answer"},
        ]
        before5 = [dict(m) for m in c5.messages]
        result5 = asyncio.run(c5.run("search for something"))
        check("excise: earlier turn message 0 untouched", c5.messages[0] == before5[0])
        check("excise: earlier turn message 1 untouched", c5.messages[1] == before5[1])
        check(
            "excise: current turn's own query preserved",
            c5.messages[2] == {"role": "user", "content": "search for something"},
        )
        this_turn_assistant5 = [m for m in c5.messages[2:] if m["role"] == "assistant"]
        check(
            "excise: exactly one assistant message for this turn",
            len(this_turn_assistant5) == 1, str(len(this_turn_assistant5)),
        )
        if this_turn_assistant5:
            kinds5 = [getattr(b, "type", None) for b in this_turn_assistant5[0]["content"]]
            check(
                "excise: dangling server_tool_use removed",
                "server_tool_use" not in kinds5, str(kinds5),
            )
            check(
                "excise: unrelated text block in the same message survives",
                "still searching" in [getattr(b, "text", None) for b in this_turn_assistant5[0]["content"]],
                str(kinds5),
            )
        check(
            "excise: never mentions /clear or a whole-turn wipe",
            "/clear" not in result5 and "undone" not in result5, result5,
        )

        # --- 6: normal multi-round conversation, regression guard
        chat_mod.MAX_TOOL_ITERATIONS = 75
        fake6 = FakeClaudeService([
            FakeResponse("tool_use", [FakeBlock("tool_use", id="a", name="bash", input={})]),
            FakeResponse("tool_use", [FakeBlock("tool_use", id="b", name="bash", input={})]),
            FakeResponse("end_turn", [FakeBlock("text", text="done for real")]),
        ])
        c6 = Chat(claude_service=fake6, clients={})  # type: ignore[arg-type]
        result6 = asyncio.run(c6.run("multi round task"))
        check(
            "normal multi-round: all three calls made", fake6.calls == 3, str(fake6.calls)
        )
        check(
            "normal multi-round: no dupes/orphans",
            not _duplicate_tool_result_ids(c6.messages)
            and not _orphaned_tool_uses(c6.messages),
        )
        check(
            "normal multi-round: real final answer returned",
            result6 == "done for real", result6,
        )

        # --- 7: cross-turn orphan repair must satisfy the API's REAL
        # "immediately after" adjacency rule, not just "answered somewhere
        # later" (which is all `_orphaned_tool_uses` itself checks). Ported
        # from ResearchMesh's smoke_test.py alongside the core/chat.py fix
        # that closes this exact gap — same bug class, same root cause: a
        # tool_use orphan survived to the start of a brand new turn
        # (nothing else after it yet), `run()` appended the new user query
        # first as always, the OLD repair then answered the orphan by
        # appending to the tail — one message too late, since the new query
        # was already sitting between the tool_use and the synthetic
        # result. The retry 400'd on the *same* id the repair had just
        # "fixed". `FakeClaudeService` above never catches this class of
        # bug because it only pops a canned script — it never actually
        # validates the message shape it's handed. This scenario uses a
        # stricter fake that does, so a regression here fails loudly
        # instead of shipping unnoticed again.
        class FakeClaudeServiceStrictAdjacency(FakeClaudeService):
            def chat(self, messages, system=None, stop_sequences=None,
                      tools=None, thinking=False):
                # Count this as a real request attempt regardless of
                # whether the adjacency check below rejects it -- matches
                # how the real API counts a 400 as a call that happened,
                # not a call that never occurred.
                self.calls += 1
                for idx, message in enumerate(messages):
                    content = message.get("content")
                    if not isinstance(content, list):
                        continue
                    for block in content:
                        kind = block.get("type") if isinstance(block, dict) else getattr(block, "type", None)
                        if not (kind == "tool_use" or (kind and kind.endswith("_tool_use"))):
                            continue
                        tool_id = block.get("id") if isinstance(block, dict) else getattr(block, "id", None)
                        nxt = messages[idx + 1] if idx + 1 < len(messages) else None
                        nxt_content = nxt.get("content") if nxt else None
                        answered = False
                        if isinstance(nxt_content, list):
                            for b2 in nxt_content:
                                k2 = b2.get("type") if isinstance(b2, dict) else getattr(b2, "type", None)
                                u2 = b2.get("tool_use_id") if isinstance(b2, dict) else getattr(b2, "tool_use_id", None)
                                if k2 and (k2 == "tool_result" or k2.endswith("_tool_result")) and u2 == tool_id:
                                    answered = True
                        if not answered:
                            raise RuntimeError(
                                f"messages.{idx}: `tool_use` ids were found "
                                f"without `tool_result` blocks immediately "
                                f"after: {tool_id}."
                            )
                # Deliberately not `super().chat()` -- that would double
                # count `self.calls`, already incremented above.
                item = self._script.pop(0)
                if isinstance(item, Exception):
                    raise item
                return item

        chat_mod.MAX_TOOL_ITERATIONS = 75
        orphan_id7 = "toolu_01R17MvTSHSQAYxEyHpvNjrK"
        fake7 = FakeClaudeServiceStrictAdjacency([
            FakeResponse("end_turn", [FakeBlock("text", text="all better now")]),
        ])
        c7 = Chat(claude_service=fake7, clients={})  # type: ignore[arg-type]
        # The orphan sitting as the very last message -- e.g. the previous
        # turn ended on a max_tokens cutoff mid tool_use (see scenario 8
        # below for why that specific trigger no longer even reaches this
        # state anymore -- this scenario proves the repair itself is
        # correct independent of how the orphan got there).
        c7.messages = [
            {"role": "user", "content": "write core/zsh_session.py"},
            {"role": "assistant", "content": "Let me write it."},
            {
                "role": "assistant",
                "content": [FakeBlock("tool_use", id=orphan_id7, name="str_replace_based_edit_tool", input={})],  # type: ignore[list-item]
            },
        ]
        result7 = asyncio.run(c7.run("you froze up again, can you continue"))
        check(
            "cross-turn repair: exactly one retry needed, not a repeated 400",
            fake7.calls == 2, str(fake7.calls),
        )
        check(
            "cross-turn repair: no orphan left behind",
            not _orphaned_tool_uses(c7.messages),
        )
        check(
            "cross-turn repair: retried request's real answer returned",
            "all better now" in result7, result7,
        )

        # --- 8: a max_tokens cutoff mid tool_use must finalize the turn
        # immediately, not silently return as if it were an ordinary
        # finished response. Ported alongside scenario 7 -- this is the
        # actual root trigger behind that scenario's bug class in
        # production: a single large `create` call (a whole new source
        # file as one tool_use) ran past the output token budget,
        # `stop_reason` came back "max_tokens" (not "tool_use"), and the
        # old code only ever routed/answered tool_use blocks when
        # `stop_reason == "tool_use"` -- so the dangling block was
        # appended to history and then just ignored, left to poison every
        # later turn.
        chat_mod.MAX_TOOL_ITERATIONS = 75
        orphan_id8 = "toolu_FRESHCUTOFF"
        fake8 = FakeClaudeService([
            FakeResponse("max_tokens", [
                FakeBlock("text", text="Let me write core/zsh_session.py"),
                FakeBlock("tool_use", id=orphan_id8, name="str_replace_based_edit_tool", input={}),
            ]),
        ])
        c8 = Chat(claude_service=fake8, clients={})  # type: ignore[arg-type]
        result8 = asyncio.run(c8.run("write core/zsh_session.py"))
        check(
            "max_tokens cutoff: exactly one call, no silent second round trip",
            fake8.calls == 1, str(fake8.calls),
        )
        check(
            "max_tokens cutoff: no orphan left behind",
            not _orphaned_tool_uses(c8.messages),
        )
        check(
            "max_tokens cutoff: does NOT silently return as if finished",
            "all resolved" not in result8 and result8 != "",
        )
        check(
            "max_tokens cutoff: reports the real cause, not a bare empty reply",
            "max_tokens" in result8 or "unresolved tool_use" in result8, result8,
        )
    finally:
        chat_mod.local_tools.execute = orig_execute
        chat_mod.ToolManager.build = orig_build  # type: ignore[method-assign]
        chat_mod.MAX_TOOL_ITERATIONS = orig_max_iter
        chat_mod.EXTRA_CONTINUATION_LIMIT = orig_extra_limit


def check_model_command() -> None:
    """`/model` / `/model swap` — config.toml wiring and index/name matching.

    Ported from ResearchMesh (see adding-model-command-to-swap-between-
    Anthropic-models.md in ResearchMesh's own /memories for the full design
    history). Covers only the ROUTER's OWN reasoning model
    (self.agent.claude_service) — nothing here touches a connected worker.

    No API call and no CliApp/prompt_toolkit involved: `load_claude_models`
    and `resolve_model_swap` (core/claude.py) are pure enough to check
    directly, the same way check_clear_and_diagnostics() above checks
    core/chat.py's diagnostics without a real conversation. core/cli.py's
    `/model` branch is a thin print/continue wrapper around these two calls,
    so covering the calls covers the actual matching logic that a bad
    index/name could otherwise silently mismatch.
    """
    print("/model command")
    from core.claude import load_claude_models, resolve_model_swap

    models = load_claude_models()
    check("claude_models is non-empty", len(models) > 0, str(models))
    check(
        "claude_models entries are all strings",
        all(isinstance(m, str) for m in models),
        str(models),
    )

    # Index matching (1-based, as shown in /model's own listing).
    check("index 1 resolves to the first entry", resolve_model_swap(models, "1") == models[0])
    last = str(len(models))
    check(
        f"index {last} resolves to the last entry",
        resolve_model_swap(models, last) == models[-1],
    )
    check("index 0 is out of range", resolve_model_swap(models, "0") is None)
    check(
        "an index past the end is out of range",
        resolve_model_swap(models, str(len(models) + 1)) is None,
    )

    # Name matching, case-insensitive, whitespace-tolerant.
    check(
        "exact name matches",
        resolve_model_swap(models, models[0]) == models[0],
    )
    check(
        "matching is case-insensitive",
        resolve_model_swap(models, models[0].upper()) == models[0],
    )
    check(
        "matching tolerates surrounding whitespace",
        resolve_model_swap(models, f"  {models[0]}  ") == models[0],
    )
    check(
        "an unrecognized name resolves to None",
        resolve_model_swap(models, "not-a-real-model") is None,
    )
    check("an empty arg resolves to None", resolve_model_swap(models, "") is None)


def check_model_refresh() -> None:
    """fetch_live_models()/refresh_claude_models() — the live-scan + TTL cache
    behind config.toml's claude_models array.

    Ported from ResearchMesh (same file/history pointer as
    check_model_command() above). Neither function is exercised by
    check_model_command() above (that one only covers the pre-existing
    load_claude_models()/resolve_model_swap()). Both accept fake
    collaborators for exactly this reason — fetch_live_models takes a
    `client`, refresh_claude_models takes `config_path`/`fetch_fn` — the
    same dependency-injection shape check_clear_and_diagnostics() above uses
    (a FakeBlock duck-typing a real content block). No network, no real
    config.toml touched, no tempfile left behind.

    refresh_claude_models's whole point is "never write on failure, only ever
    write on a successful scan" — that is asserted directly here (byte-for-
    byte file comparison before/after), not just exercised incidentally, so a
    future edit that weakens that guarantee fails loudly instead of only
    showing up as a mystery CI config.toml diff months later. tomlkit is
    imported lazily inside refresh_claude_models() only on a successful
    scan's write — if it isn't installed (true for this repo's own CI
    dependency set, see the module docstring above), the success-path write
    is skipped in favour of a documented fallback (return the fresh result,
    persist nothing), and this check verifies whichever behaviour is
    actually correct for the environment it's running in, rather than
    assuming tomlkit is present.
    """
    print("model refresh (fetch_live_models / refresh_claude_models)")
    import importlib.util
    import tomllib
    from datetime import UTC, datetime, timedelta

    from core.claude import fetch_live_models, refresh_claude_models

    has_tomlkit = importlib.util.find_spec("tomlkit") is not None

    # --- fetch_live_models(): pure grouping/sorting logic, fake client -----

    class FakeModel:
        def __init__(self, model_id: str, created_at: datetime):
            self.id = model_id
            self.created_at = created_at

    class FakeModelsResource:
        def __init__(self, models: list):
            self._models = models

        def list(self):
            return list(self._models)

    class FakeClient:
        def __init__(self, models: list):
            self.models = FakeModelsResource(models)

    now = datetime.now(UTC)

    # Sonnet exists but is NOT the newest release overall — it must still
    # end up first, ahead of the genuinely newest family (opus here).
    mixed = FakeClient(
        [
            FakeModel("claude-opus-9", now),
            FakeModel("claude-opus-8", now - timedelta(days=30)),
            FakeModel("claude-sonnet-9", now - timedelta(days=5)),
            FakeModel("claude-sonnet-8", now - timedelta(days=40)),
            FakeModel("claude-haiku-9", now - timedelta(days=10)),
            FakeModel("not-a-claude-id-at-all", now),  # must be skipped, not crash
        ]
    )
    result = fetch_live_models(client=mixed)  # type: ignore[arg-type]
    check(
        "sonnet is forced first even when not newest",
        result[0] == "claude-sonnet-9",
        str(result),
    )
    check(
        "one entry per family, newest kept",
        set(result) == {"claude-sonnet-9", "claude-opus-9", "claude-haiku-9"},
        str(result),
    )
    check(
        "non-family-matching ids are silently skipped",
        "not-a-claude-id-at-all" not in result,
        str(result),
    )
    check(
        "remaining families stay in pure recency order",
        result[1:] == ["claude-opus-9", "claude-haiku-9"],
        str(result),
    )

    # No sonnet family at all -> pure recency order, no reordering applied.
    no_sonnet = FakeClient(
        [
            FakeModel("claude-opus-1", now - timedelta(days=1)),
            FakeModel("claude-haiku-1", now - timedelta(days=2)),
        ]
    )
    check(
        "with no sonnet family, order is pure recency",
        fetch_live_models(client=no_sonnet) == ["claude-opus-1", "claude-haiku-1"],  # type: ignore[arg-type]
    )

    # --- refresh_claude_models(): TTL cache / write-on-success-only --------

    def write_config(path: Path, *, models: list, checked_at, ttl_hours=24) -> None:
        lines = ["[claude]", f"claude_models = {models!r}".replace("'", '"')]
        if checked_at is not None:
            lines.append(f'claude_models_checked_at = "{checked_at}"')
        lines.append(f"model_scan_ttl_hours = {ttl_hours}")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def read_models(path: Path) -> list:
        with open(path, "rb") as f:
            return tomllib.load(f).get("claude", {}).get("claude_models")

    tmp_dir = Path("/tmp")
    fresh_iso = now.isoformat()
    stale_iso = (now - timedelta(hours=48)).isoformat()

    # 1) TTL fresh -> no scan attempted at all, cache returned untouched.
    cfg = tmp_dir / "smoke_model_refresh_fresh.toml"
    write_config(cfg, models=["cached-a", "cached-b"], checked_at=fresh_iso)
    before = cfg.read_text(encoding="utf-8")
    called = {"n": 0}

    def should_not_be_called() -> list:
        called["n"] += 1
        raise AssertionError("fetch_fn should not run when the TTL is fresh")

    try:
        out = refresh_claude_models(config_path=cfg, fetch_fn=should_not_be_called)
        check("fresh TTL returns the cached array", out == ["cached-a", "cached-b"], str(out))
        check("fresh TTL never calls fetch_fn", called["n"] == 0)
        check("fresh TTL leaves the file untouched", cfg.read_text(encoding="utf-8") == before)
    finally:
        cfg.unlink(missing_ok=True)

    # 2) TTL stale + scan succeeds -> array + timestamp updated (if tomlkit
    #    is installed) or the fresh result is still returned but not
    #    persisted (if it isn't) — either way is the documented contract.
    cfg = tmp_dir / "smoke_model_refresh_success.toml"
    write_config(cfg, models=["old-a"], checked_at=stale_iso)
    before = cfg.read_text(encoding="utf-8")
    try:
        out = refresh_claude_models(
            config_path=cfg, fetch_fn=lambda: ["fresh-x", "fresh-y"]
        )
        check(
            "stale + successful scan returns the fresh array",
            out == ["fresh-x", "fresh-y"],
            str(out),
        )
        if has_tomlkit:
            check(
                "successful scan persists the fresh array",
                read_models(cfg) == ["fresh-x", "fresh-y"],
                str(read_models(cfg)),
            )
            check(
                "successful scan updates claude_models_checked_at",
                "claude_models_checked_at" in cfg.read_text(encoding="utf-8"),
            )
        else:
            check(
                "without tomlkit, a successful scan still isn't persisted",
                cfg.read_text(encoding="utf-8") == before,
            )
    finally:
        cfg.unlink(missing_ok=True)

    # 3) TTL stale (well past due) + scan fails -> config untouched, old
    #    cache returned. This is the exact CI/placeholder-key scenario.
    cfg = tmp_dir / "smoke_model_refresh_failure.toml"
    write_config(cfg, models=["old-cached"], checked_at=stale_iso)
    before = cfg.read_text(encoding="utf-8")

    def failing_fetch() -> list:
        raise RuntimeError("simulated network/auth failure")

    try:
        out = refresh_claude_models(config_path=cfg, force=True, fetch_fn=failing_fetch)
        check("a failed scan returns the old cached array", out == ["old-cached"], str(out))
        check(
            "a failed scan leaves the file byte-for-byte untouched",
            cfg.read_text(encoding="utf-8") == before,
        )
    finally:
        cfg.unlink(missing_ok=True)

    # 4) force=True bypasses an otherwise-fresh TTL.
    cfg = tmp_dir / "smoke_model_refresh_forced.toml"
    write_config(cfg, models=["cached-only"], checked_at=fresh_iso)
    try:
        out = refresh_claude_models(config_path=cfg, force=True, fetch_fn=lambda: ["forced"])
        check("force=True overrides a fresh TTL", out == ["forced"], str(out))
    finally:
        cfg.unlink(missing_ok=True)

    # 5) A malformed timestamp is treated as stale, not fatal.
    cfg = tmp_dir / "smoke_model_refresh_malformed.toml"
    write_config(cfg, models=["cached-only"], checked_at="not-a-real-timestamp")
    try:
        out = refresh_claude_models(config_path=cfg, fetch_fn=lambda: ["rescanned"])
        check(
            "a malformed checked_at is treated as stale, not fatal",
            out == ["rescanned"],
            str(out),
        )
    finally:
        cfg.unlink(missing_ok=True)


def check_model_worker_dispatch() -> None:
    """`/model <worker> [swap <name/index>]` — the worker-reach-in half of
    `/model`, which check_model_command() above explicitly does NOT cover
    (its own docstring says so — it only tests the ROUTER's own reasoning
    model). This is the one piece of the whole /model feature that is a
    genuine cross-process contract: a live Router asking a live, separately
    -versioned ResearchMesh worker to change its own state over MCP.

    Covers Chat.resolve_worker_model_request() (sync, pure — the
    precedence-critical worker-name-vs-subcommand-name decision) and
    Chat.call_worker_model() (async — the actual fallible MCP call plus
    response formatting), both extracted from core/cli.py's `/model` branch
    specifically so this is testable without a live REPL or a real worker
    subprocess — see adding-model-command-to-swap-between-Anthropic-
    models.md in ResearchMesh's own /memories, Section 3G, for the full
    story of why this gap existed and was closed.

    In particular, this is what regression-protects the adversarial case
    the user asked to be reviewed live earlier (a worker literally named
    "swap") — that was previously verified only via a one-off
    interactive_run session, never captured as a repeatable check.
    """
    print("/model <worker> dispatch (resolve_worker_model_request / call_worker_model)")
    from core.chat import Chat
    from core.claude import Claude

    workers = {
        "alpha": FakeWorker(["delegate", "model"]),
        # A worker deliberately named the same as /model's own subcommand —
        # the precedence test. Worker-name-wins must still hold: `/model
        # swap` should list THIS worker, not be mistaken for the router's
        # own `/model swap <name/index>`.
        "swap": FakeWorker(["delegate", "model"]),
    }
    chat = Chat(claude_service=cast(Claude, None), clients=workers)

    # --- resolve_worker_model_request: precedence + arg-parsing ---------

    check(
        "an unknown name is not treated as a worker",
        chat.resolve_worker_model_request("nonexistent-worker", "") is None,
    )
    check(
        "an unknown name falls through even with a swap-shaped arg",
        chat.resolve_worker_model_request("nonexistent-worker", "swap 2") is None,
    )

    check(
        "a bare worker name resolves to a list request",
        chat.resolve_worker_model_request("alpha", "")
        == ("alpha", {"action": "list"}, None),
    )
    check(
        "worker + swap + arg resolves to a swap request",
        chat.resolve_worker_model_request("alpha", "swap 2")
        == ("alpha", {"action": "swap", "arg": "2"}, None),
    )
    check(
        "worker + swap + a multi-word arg keeps it whole",
        chat.resolve_worker_model_request("alpha", "swap claude opus")
        == ("alpha", {"action": "swap", "arg": "claude opus"}, None),
    )

    no_arg = chat.resolve_worker_model_request("alpha", "swap")
    check(
        "worker + swap with no arg rejects locally, no call implied",
        no_arg is not None and no_arg[1] is None,
        str(no_arg),
    )
    check(
        "the no-arg rejection names the worker in its usage message",
        no_arg is not None and "alpha" in (no_arg[2] or ""),
        str(no_arg),
    )

    garbage = chat.resolve_worker_model_request("alpha", "nonsense")
    check(
        "worker + an unrecognized subcommand rejects locally",
        garbage is not None and garbage[1] is None,
        str(garbage),
    )
    check(
        "the unrecognized-subcommand message names the bad subcommand",
        garbage is not None and "nonsense" in (garbage[2] or ""),
        str(garbage),
    )

    # The precedence case: a worker literally named "swap".
    worker_swap_list = chat.resolve_worker_model_request("swap", "")
    check(
        "a worker named 'swap' still resolves as a worker (bare list)",
        worker_swap_list == ("swap", {"action": "list"}, None),
        str(worker_swap_list),
    )
    worker_swap_swap = chat.resolve_worker_model_request("swap", "swap 3")
    check(
        "a worker named 'swap' with its OWN 'swap' subcommand still parses",
        worker_swap_swap == ("swap", {"action": "swap", "arg": "3"}, None),
        str(worker_swap_swap),
    )
    worker_swap_noarg = chat.resolve_worker_model_request("swap", "swap")
    check(
        "worker 'swap' + subcommand 'swap' with no arg rejects locally, "
        "not confused with the double name",
        worker_swap_noarg is not None and worker_swap_noarg[1] is None,
        str(worker_swap_noarg),
    )

    # --- call_worker_model: the actual (fallible) MCP call --------------

    async def go() -> dict[str, str]:
        results: dict[str, str] = {}

        listing_worker = FakeWorker(
            ["model"],
            call_tool_result=CallToolResult(
                content=[
                    TextContent(
                        type="text",
                        text="[model: available]\n  1. sonnet  (current)",
                    )
                ]
            ),
        )
        chat_ok = Chat(
            claude_service=cast(Claude, None), clients={"w": listing_worker}
        )
        results["success"] = await chat_ok.call_worker_model(
            "w", {"action": "list"}
        )

        failing_worker = FakeWorker(
            ["model"], call_tool_error=ConnectionError("worker is down")
        )
        chat_fail = Chat(
            claude_service=cast(Claude, None), clients={"w": failing_worker}
        )
        results["transport error"] = await chat_fail.call_worker_model(
            "w", {"action": "list"}
        )

        empty_ok_worker = FakeWorker(
            ["model"], call_tool_result=CallToolResult(content=[])
        )
        chat_empty_ok = Chat(
            claude_service=cast(Claude, None), clients={"w": empty_ok_worker}
        )
        results["empty, not an error"] = await chat_empty_ok.call_worker_model(
            "w", {"action": "list"}
        )

        empty_err_worker = FakeWorker(
            ["model"],
            call_tool_result=CallToolResult(content=[], is_error=True),
        )
        chat_empty_err = Chat(
            claude_service=cast(Claude, None), clients={"w": empty_err_worker}
        )
        results["empty, an error"] = await chat_empty_err.call_worker_model(
            "w", {"action": "list"}
        )

        return results

    try:
        results = asyncio.run(asyncio.wait_for(go(), timeout=30))
    except Exception as e:
        check(
            "call_worker_model scenarios complete",
            False,
            f"{type(e).__name__}: {e}",
        )
        return
    check("call_worker_model scenarios complete", True)

    check(
        "a successful call is prefixed with the worker id",
        results["success"].startswith("[worker: w] "),
        results["success"],
    )
    check(
        "a successful call's body is passed through",
        "[model: available]" in results["success"],
        results["success"],
    )

    check(
        "a transport error is caught, not raised, and reported per-worker",
        results["transport error"]
        == "[worker: w] model tool call failed: worker is down",
        results["transport error"],
    )

    check(
        "an empty, non-error result reports '(no output)'",
        results["empty, not an error"] == "[worker: w] (no output)",
        results["empty, not an error"],
    )
    check(
        "an empty, is_error result reports '(error, no message text)'",
        results["empty, an error"] == "[worker: w] (error, no message text)",
        results["empty, an error"],
    )


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
        check_docs_match_code,
        check_dagent_and_workers,
        check_clear_and_diagnostics,
        check_run_loop_tool_use_lifecycle,
        check_model_command,
        check_model_refresh,
        check_model_worker_dispatch,
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
