"""A stand-in ResearchMesh worker: one `delegate` tool, over stdio.

Launched by `e2e_test.py`; not part of the app. Faithful where it matters —
the same tool name, and the *same description text* in every instance, which is
exactly the collision a real fleet produces. `mcp_server.py` in ResearchMesh
hardcodes a single `_DELEGATE_DESCRIPTION` constant, so two real workers are
indistinguishable in precisely this way.

It makes no Anthropic call of its own, so the test costs nothing on the worker
side, and it does not start ResearchMesh's own configured MCP servers — pointing
the test at a real worker would launch whatever that machine's config.toml
declares (a Unity relay, an Unreal bridge), which a test has no business doing.

The 2s sleep is load-bearing for the fan-out half of the test: two of these run
sequentially take ~4s, concurrently ~2s.
"""

import asyncio
import os
import sys
import time

from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

NAME = os.getenv("FAKE_WORKER_NAME", "worker")

# Deliberately identical in every instance. Do not "improve" this by varying it
# per worker — that would quietly delete the thing the test exists to prove.
DESCRIPTION = (
    "Hand a task to ResearchMesh, a full agent running on this machine, and get "
    "back its final answer. It runs its own multi-step loop."
)

TOOLS = [
    types.Tool(
        name="delegate",
        description=DESCRIPTION,
        input_schema={
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "What you want done."},
                "session": {"type": "string", "description": "Conversation id."},
            },
            "required": ["task"],
        },
    )
]


async def list_tools(ctx, params) -> types.ListToolsResult:
    return types.ListToolsResult(tools=TOOLS)


async def call_tool(ctx, params) -> types.CallToolResult:
    task = (params.arguments or {}).get("task", "")
    started = time.time()
    # stderr, not stdout: on stdio, fd 1 is the JSON-RPC channel.
    print(f"[{NAME}] delegate started: {task[:60]!r}", file=sys.stderr)
    await asyncio.sleep(2)
    print(
        f"[{NAME}] delegate done in {time.time() - started:.1f}s", file=sys.stderr
    )
    return types.CallToolResult(
        content=[
            types.TextContent(
                type="text",
                text=f"worker {NAME} completed the task. Task text was: {task}",
            )
        ]
    )


server = Server(f"fake-{NAME}", on_list_tools=list_tools, on_call_tool=call_tool)


async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream, server.create_initialization_options()
        )


if __name__ == "__main__":
    asyncio.run(main())
