"""End-to-end check against two live MCP workers. Costs real API tokens.

    python e2e_test.py

Not part of the standard gates: it needs ANTHROPIC_API_KEY, makes real requests
and takes about 15s. Run it after touching `core/tools.py`, `core/chat.py`,
`mcp_client.py` or how a turn reaches a worker.

It checks what `smoke_test.py` can only assert against fakes:

  1. the API rejects the un-namespaced tool list and accepts the namespaced
  2. namespacing survives a real MCP transport
  3. the model calls both workers in one turn, from description headers alone

Two `e2e_worker.py` subprocesses stand in for the fleet.
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

        # `count_tokens` works here only because `index.tool_defs` is
        # worker-only. It cannot validate the array the router sends, which
        # also carries `web_search`/`web_fetch`: the endpoint answers "Server
        # tools are not supported in the count_tokens endpoint", a 400 that
        # looks like a tool-list problem. Including local_tools.TOOLS needs a
        # real `beta.messages.create` call.
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
        # Containment, not equality: the router has local tools of its own, so
        # the model may call one this turn, which says nothing about the claim
        # under test (both workers were called from their description headers
        # alone).
        check(
            "the model called both workers from the descriptions alone",
            set(names) <= used,
            f"called {sorted(used)}, missing {sorted(set(names) - used)}",
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
