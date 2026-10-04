"""A stand-in ResearchMesh worker: one `delegate` tool, over stdio.

Launched by `e2e_test.py`; not part of the app. Every instance has the same
tool name and the same description text, which is the collision a real fleet
produces (`mcp_server.py` in ResearchMesh hardcodes one
`_DELEGATE_DESCRIPTION`). It makes no Anthropic call and does not start a real
worker's configured MCP servers (a Unity relay, an Unreal bridge), which a test
should not launch.

The 2s sleep matters for the fan-out check: two workers take ~4s sequentially
and ~2s concurrently.
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
