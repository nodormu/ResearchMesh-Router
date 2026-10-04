import asyncio
import json
import sys
from contextlib import AsyncExitStack
from typing import Any, Literal

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import (
    create_mcp_http_client,
    streamable_http_client,
)

Transport = Literal["stdio", "sse", "http"]


class MCPClient:
    """MCP client supporting stdio, SSE and Streamable HTTP transports.

    - stdio: spawns a local server process (`command` + `args`).
    - sse:   connects to a remote server's SSE endpoint (`url`).
    - http:  connects to a remote server's Streamable HTTP endpoint (`url`).

    For the remote transports `headers` may carry auth, e.g. {"Authorization":
    "Bearer <token>"}.

    `timeout_seconds` is this fork's addition and should be set: the defaults
    are too short for a worker that runs a whole agentic loop before replying
    (`create_mcp_http_client` reads for 300s; one `delegate` call driving a GUI
    can run for many minutes). It applies in two places that time out
    independently: the httpx2 read timeout (waiting on the HTTP response) and
    ClientSession's `read_timeout_seconds` (the MCP per-request deadline; a
    plain float in mcp 2.x, a timedelta in 1.x).
    """

    def __init__(
        self,
        command: str | None = None,
        args: list[str] | None = None,
        env: dict | None = None,
        *,
        url: str | None = None,
        transport: Transport = "stdio",
        headers: dict[str, str] | None = None,
        timeout_seconds: float | None = None,
    ):
        self._command = command
        self._args = args or []
        self._env = env
        self._url = url
        self._transport = transport
        self._headers = headers
        self._timeout_seconds = timeout_seconds
        self._session: ClientSession | None = None
        self._exit_stack: AsyncExitStack = AsyncExitStack()

    async def connect(self):
        if self._transport == "stdio":
            read, write = await self._connect_stdio()
        elif self._transport == "sse":
            read, write = await self._connect_sse()
        elif self._transport == "http":
            read, write = await self._connect_http()
        else:
            raise ValueError(f"Unknown transport: {self._transport!r}")

        self._session = await self._exit_stack.enter_async_context(
            ClientSession(
                read, write, read_timeout_seconds=self._timeout_seconds
            )
        )
        await self._session.initialize()

    async def _connect_stdio(self):
        if not self._command:
            raise ValueError("stdio transport requires a `command`")
        server_params = StdioServerParameters(
            command=self._command,
            args=self._args,
            env=self._env,
        )
        read, write = await self._exit_stack.enter_async_context(
            stdio_client(server_params)
        )
        return read, write

    async def _connect_sse(self):
        if not self._url:
            raise ValueError("sse transport requires a `url`")
        read, write = await self._exit_stack.enter_async_context(
            sse_client(self._url, headers=self._headers)
        )
        return read, write

    async def _connect_http(self):
        if not self._url:
            raise ValueError("http transport requires a `url`")
        # Streamable HTTP is what most remote MCP servers expose. mcp 2.0
        # renamed it from `streamablehttp_client` and dropped its `headers=`
        # argument: HTTP settings now come from an httpx2 client built here.
        # `create_mcp_http_client` is the SDK's factory, so the recommended MCP
        # timeouts still apply; a bare `httpx2.AsyncClient(headers=...)` would
        # drop them. A passed-in client's lifecycle is ours (the transport
        # closes only a client it created), hence it is entered on the exit
        # stack.
        http_client = None
        if self._headers or self._timeout_seconds:
            timeout = None
            if self._timeout_seconds:
                # httpx2 is imported here rather than at module scope so a
                # problem with it can only break the http transport, not stdio
                # as well. It arrives as a dependency of mcp; nothing installs
                # it directly.
                import httpx2

                # Long read, short connect: the read budget is for a worker
                # busy for minutes; the connect budget is for a worker that is
                # switched off, which should fail in seconds.
                timeout = httpx2.Timeout(
                    float(self._timeout_seconds), connect=15.0
                )
            http_client = await self._exit_stack.enter_async_context(
                create_mcp_http_client(headers=self._headers, timeout=timeout)
            )
        read, write = await self._exit_stack.enter_async_context(
            streamable_http_client(self._url, http_client=http_client)
        )
        return read, write

    def session(self) -> ClientSession:
        if self._session is None:
            raise ConnectionError(
                "Client session not initialized. Call connect() first."
            )
        return self._session

    async def list_tools(self) -> list[types.Tool]:
        result = await self.session().list_tools()
        return result.tools

    async def call_tool(
        self, tool_name: str, tool_input
    ) -> types.CallToolResult | None:
        return await self.session().call_tool(tool_name, tool_input)

    async def list_prompts(self) -> list[types.Prompt]:
        result = await self.session().list_prompts()
        return result.prompts

    async def get_prompt(self, prompt_name, args: dict[str, str]):
        result = await self.session().get_prompt(prompt_name, args)
        return result.messages

    async def read_resource(self, uri: str) -> Any:
        # 2.0 takes a plain `str` here; 1.x wanted a pydantic `AnyUrl`.
        result = await self.session().read_resource(uri)
        resource = result.contents[0]  # only the first content is used

        if isinstance(resource, types.TextResourceContents):
            if resource.mime_type == "application/json":
                return json.loads(resource.text)

            return resource.text  # fallback: return as plain text

    async def cleanup(self):
        await self._exit_stack.aclose()
        self._session = None

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.cleanup()


# Standalone check of every server in config.toml — connect, list tools, exit:
#   python mcp_client.py
# Or test one endpoint without touching the config:
#   MCP_URL=http://host:8000/mcp MCP_TOKEN=<token> python mcp_client.py
async def main():
    import os

    # Imported inside the function because main.py imports this module (a
    # module-level import would be circular). Reusing `build_client` and
    # `_expand_paths` makes this standalone check build each client exactly as
    # the app does: a hand-built client forced `transport="http"` on every
    # entry and never expanded `~`/`$USER`, so a stdio entry failed on a
    # missing URL.
    import main as app

    override_headers = None
    if os.getenv("MCP_URL"):
        servers = [{"name": "MCP_URL", "url": os.getenv("MCP_URL")}]
        if os.getenv("MCP_TOKEN"):
            override_headers = {
                "Authorization": f"Bearer {os.getenv('MCP_TOKEN')}"
            }
    else:
        servers = app.MCP_SERVERS

    if not servers:
        print("No servers configured under [mcp] in config.toml.")
        return

    if not app.MCP_ENABLED:
        # Checking them anyway: this command exists to tell you whether a server
        # *would* work, and `enabled = false` is usually why the app isn't
        # using one you expected it to.
        print("[mcp] enabled = false in config.toml — the app skips all of these.")

    for index, server in enumerate(servers):
        name = server.get("name") or f"server_{index}"

        if not server.get("enabled", True):
            print(f"\n{name}: disabled in config.toml — skipped")
            continue

        server = app._expand_paths(server)
        target = server.get("url") or " ".join(server.get("command") or [])
        try:
            client = (
                MCPClient(
                    transport="http", url=server["url"], headers=override_headers
                )
                if override_headers
                else app.build_client(server, name)
            )
            async with client:
                tools = await client.list_tools()
                print(f"\n{name}: {target} — {len(tools)} tool(s)")
                for tool in tools:
                    print(f"  - {tool.name}: {tool.description}")
        except BaseException as e:
            print(f"\n{name}: {target} — FAILED ({type(e).__name__}: {e})")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(main())
