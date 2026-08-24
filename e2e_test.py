"""End-to-end check against two live MCP workers. Costs real API tokens.

    python e2e_test.py

Unlike `smoke_test.py` this is **not** part of the standard gates: it needs
ANTHROPIC_API_KEY, makes real requests, and takes ~15s. Run it when you touch
`core/tools.py`, `mcp_client.py`, or anything about how a turn reaches a worker.

It exists because `smoke_test.py` proves the router's invariants against fakes
inside one process, which leaves three claims resting on assertion alone:

  1. that the duplicate-name failure is real. `smoke_test.py` enforces the API's
     tool-name rule locally, from a regex. This sends both lists to the API and
     confirms the namespaced one is accepted *and* that the un-namespaced one is
     rejected — so the premise this whole repo is built on stays falsifiable
     rather than becoming folklore in a comment;
  2. that namespacing survives a real MCP transport, not just a Python object
     pretending to be one;
  3. that a real model, given only the `[worker: ...]` description headers,
     actually issues both calls in a single turn — which is what makes the
     fan-out reachable at all. Correct plumbing that the model never triggers
     would pass every check in `smoke_test.py`.

Two `e2e_worker.py` subprocesses stand in for the fleet. See that file for why
it isn't pointed at a real ResearchMesh install.
"""

import asyncio
import os
import sys
import time
from contextlib import AsyncExitStack
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from anthropic import Anthropic

from core.chat import Chat
from core.claude import Claude
from core.tools import ToolManager
from mcp_client import MCPClient

MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5")
WORKER_SCRIPT = str(ROOT / "e2e_worker.py")

# Two workers whose *tools* are identical and whose *roles* are not — the case
# the router exists to make navigable. The second name is deliberately illegal
# for a tool name, to prove sanitisation survives a real connection.
DESCRIPTIONS = {
    "gpu-box": (
        "Headless Linux with the CUDA stack and the training data in /data. "
        "No display at all, so GUI work fails here."
    ),
    "win.box 2": (
        "Windows desktop with a real display, Excel, and the label printer "
        "attached. Use it for anything that must be seen or printed."
    ),
}

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}{' — ' + detail if detail else ''}")
    if not ok:
        FAILURES.append(label)


def build_worker(name: str) -> MCPClient:
    return MCPClient(
        command=sys.executable,
        args=[WORKER_SCRIPT],
        env={**os.environ, "FAKE_WORKER_NAME": name},
        transport="stdio",
        timeout_seconds=120,
    )


def tool_names_used(messages) -> set[str]:
    """Every tool name the model actually called, across the conversation."""
    used: set[str] = set()
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            name = getattr(block, "name", None)
            if name:
                used.add(name)
    return used


async def main() -> int:
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set — this test makes real API calls.")
        return 1

    async with AsyncExitStack() as stack:
        clients: dict[str, MCPClient] = {}
        for name in DESCRIPTIONS:
            client = build_worker(name)
            await client.connect()
            stack.push_async_callback(client.cleanup)
            clients[name] = client
        print(f"connected {len(clients)} workers over stdio\n")

        print("tool index")
        index = await ToolManager.build(clients, DESCRIPTIONS)
        names = [t["name"] for t in index.tool_defs]
        check(
            "two identical workers yield two distinct tools",
            len(set(names)) == 2,
            str(names),
        )
        check(
            "an illegal worker id is sanitised over a real connection",
            "win_box_2__delegate" in names,
            str(names),
        )

        print("\nthe premise: duplicate tool names are rejected by the API")
        api = Anthropic()

        def count(tools) -> tuple[bool, str]:
            try:
                api.messages.count_tokens(
                    model=MODEL,
                    messages=[{"role": "user", "content": "hi"}],
                    tools=tools,
                )
                return True, ""
            except Exception as e:
                return False, str(e)

        accepted, error = count(index.tool_defs)
        check("namespaced list is accepted", accepted, error[:120])

        # What ResearchMesh's own bridge would have sent.
        raw = [dict(t, name="delegate") for t in index.tool_defs]
        accepted, error = count(raw)
        check(
            "un-namespaced list is rejected",
            not accepted and "unique" in error.lower(),
            error[:120] if not accepted else "it was accepted?!",
        )

        print("\nlive router turn")
        chat = Chat(
            claude_service=Claude(model=MODEL),
            clients=clients,
            descriptions=DESCRIPTIONS,
            max_parallel=8,
        )
        started = time.time()
        answer = await chat.run(
            "Ask both workers, at the same time, to report their status. "
            "Send each one a single delegation with the task text 'report status'. "
            "Then tell me what each replied."
        )
        elapsed = time.time() - started
        print(f"\n--- router answer ({elapsed:.1f}s) ---\n{answer}\n---")

        used = tool_names_used(chat.messages)
        check(
            "the model called both workers from the descriptions alone",
            used == set(names),
            f"called {sorted(used)}",
        )
        check(
            "both workers' replies reached the model",
            "gpu-box" in answer and "win" in answer.lower(),
            answer[:120],
        )
        # The workers sleep 2s each. Sequential is >=4s of sleep plus two model
        # round trips; concurrent is ~2s plus the same. The margin is wide
        # because model latency dominates and varies.
        check(
            "workers ran concurrently",
            elapsed < 12,
            f"{elapsed:.1f}s — check whether fan-out regressed to sequential",
        )

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): {', '.join(FAILURES)}")
        return 1
    print("end-to-end checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
