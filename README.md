# ResearchMesh-Router

A command-line agent that owns no tools and executes nothing. Every capability it
has belongs to an MCP server on another machine. Its entire job is deciding which
machine should do what, saying so clearly, and running independent work at the
same time.

It is built for fleets of **interchangeable** workers — several machines running
the same agent software, differing only in what is installed on them, attached to
them, or stored on them. That is the case the obvious approach breaks on, which
is why this is its own program rather than a flag on an ordinary MCP client.

## The problem it solves

Identical MCP servers collide. An agent served over MCP typically exposes one
broad entry-point tool — [ResearchMesh](https://github.com/nodormu/ResearchMesh)
exposes exactly one, `delegate` — so pointing three instances at one client makes
all three advertise the same tool name. The Anthropic API rejects that request
outright:

```
400 invalid_request_error: tools: Tool names must be unique.
```

A straightforward MCP bridge passes each tool's name through verbatim and
resolves owners first-wins, so a fleet of identical workers fails twice over: the
request 400s, and even if it didn't, only the first worker would ever be
reachable — silently, since nothing reports the loser.

This program is that bridge rebuilt for many workers. It namespaces every tool
per worker, gives each worker an identity the model can actually route on, and
runs different workers concurrently. See [core/tools.py](core/tools.py).

**If Claude Code is your front end, you do not need this.** It namespaces MCP
tools as `mcp__<server>__<tool>` already, so it can drive any number of identical
workers with no changes to anything. This exists for the case where the
orchestrator is itself a CLI agent you control.

Nothing here is ResearchMesh-specific. The namespacing, the per-worker
descriptions and the fan-out all work against any MCP server; ResearchMesh is
simply the worker it was built and tested against.

## Quick start

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=...          # in practice lives in ~/.bashrc
export RESEARCHMESH_MCP_TOKEN=...     # one per worker, named by its token_env
python main.py                        # from the repo root
```

Then describe your fleet in `config.toml` (every entry there is a commented-out
example; replace them with your machines).

On each worker machine, run ResearchMesh as a server:

```bash
# on the worker
export RESEARCHMESH_MCP_TOKEN=...
python mcp_server.py --transport streamable-http --host 0.0.0.0 --port 8100
```

## Configuring the fleet

```toml
[router]
max_parallel    = 8      # how many workers may be busy at once
timeout_seconds = 900    # default deadline for one call to a worker

[mcp]
enabled = true
servers = [
  { name = "gpu-box",
    url = "http://192.168.2.31:8100/mcp/",
    token_env = "GPU_BOX_MCP_TOKEN",
    description = "Headless Linux, no display — GUI tasks fail here. CUDA stack and the datasets in /data.",
    timeout_seconds = 3600 },
]
```

Three fields deserve more than a passing glance.

**`name` becomes the tool-name prefix.** Tools are declared to Claude as
`<name>__<tool>`, so `gpu-box__delegate`. Keep it short, and stick to letters,
digits, `_` and `-` — anything else is substituted with `_` to satisfy the API's
`^[a-zA-Z0-9_-]{1,128}$` rule, which means `gpu box` and `gpu-box` would collide.

**`description` is the field you cannot skip.** Namespacing gives two workers
distinct *names*; it does nothing about the fact that ResearchMesh hardcodes a
single `_DELEGATE_DESCRIPTION` constant, so every worker in your fleet describes
itself with byte-identical text. Without a description the model has nothing to
route on and will pick more or less at random. Write what is true of that machine
specifically and would change the decision: its OS and session type, what is
installed, what is physically attached, what data is on it, what it must not be
used for. It is prepended to every one of that worker's tools as
`[worker: name] ...`.

It lives here rather than on the worker on purpose: it describes the machine's
role *in this fleet*, which the machine has no way to know, and changing it needs
no redeploy. It also works against MCP servers that aren't ResearchMesh at all.

**`timeout_seconds` defaults matter.** The MCP SDK's own default read timeout is
300s. A worker driving a desktop GUI routinely runs longer than that, and when
the timeout fires the work is already done on the far side and simply lost. The
default here is 900s to match the timeout the reference Claude Code config uses
against the same server; raise it per worker for long compute. The *connect*
timeout stays at 15s regardless, so a machine that is switched off fails in
seconds instead of hanging the turn for a quarter of an hour.

## How work is distributed

Tool calls are grouped by owning worker. The groups run concurrently; calls
within a group run in order.

That asymmetry is not a compromise, it mirrors the workers. A ResearchMesh worker
serialises `delegate` behind an `asyncio.Lock` because it has one mouse, one
browser page and one IPython kernel. Issuing two calls at once to the same worker
would not make it faster — the second would sit on that lock, burning its
timeout. So `max_parallel` is effectively "how many machines at once".

## Environment variables

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Read from the shell. The app does not load a `.env`. |
| `CLAUDE_MODEL` | Overrides `[claude] model` in `config.toml`. |
| `CLAUDE_SHOW_USAGE=1` | Print per-request token and prompt-cache counters. |
| *(per worker)* | Each `token_env` names the variable holding that worker's bearer token. A worker with no `token_env` connects unauthenticated. |

Tokens are never stored in `config.toml`, which is committed. Generate one with
`python -c "import secrets; print(secrets.token_urlsafe(32))"`.

## Checks

Three gates, the same bar as ResearchMesh:

```bash
ruff check .        # should be clean
mypy .              # should be clean
python smoke_test.py
```

`smoke_test.py` needs no API key, no network and no running workers. It builds a
fleet of fakes and asserts the things that break silently: that two workers
exposing the same tool name produce two distinct, API-legal names; that the
namespacing round-trips so the worker is called with its own bare tool name; that
a dead worker is skipped rather than taking the fleet down; that groups fan out
while a single worker's calls stay serial; and that every `tool_use` block gets
exactly one `tool_result`, in order.

That last one is not fussiness. An unanswered `tool_use` block poisons every
later request in the session with a 400 about unresolved ids, long after the turn
that caused it.

There is a fourth check, deliberately outside the gates because it spends real
tokens:

```bash
python e2e_test.py     # ~15s, needs ANTHROPIC_API_KEY
```

It launches two workers over real stdio MCP and covers what fakes cannot: that
the duplicate-name 400 is actually the API's behaviour rather than a claim in a
comment, that namespacing survives a real transport, and that a real model —
given nothing but the `[worker: …]` headers — issues both calls in one turn.
Plumbing that fans out perfectly but which the model never triggers would pass
every offline check.

## Origin

This is a standalone project with its own history, but it did not start from
nothing: the CLI shell, the Anthropic wrapper and the MCP client began as copies
from [ResearchMesh](https://github.com/nodormu/ResearchMesh) (same author, MIT).
`core/cli.py` is still unchanged from it.

Worth knowing as a maintainer rather than as trivia: `mcp_client.py` and
`core/tools.py` are the two files that broke on the mcp 1.x → 2.x major, and
their equivalents in ResearchMesh broke the same way. The next SDK major will
land in both. There is no shared git ancestry to cherry-pick across, so that is
a manual port — worth glancing at how the other project solved it before solving
it again here.

## Project layout

```
main.py           entrypoint — loads config, connects the fleet, runs the REPL
mcp_client.py     MCP client (stdio / SSE / Streamable HTTP)
config.toml       the fleet, and router behaviour
smoke_test.py     the offline gate
e2e_test.py       live check against real workers (costs tokens, not a gate)
e2e_worker.py     a stand-in worker the e2e test launches
core/
  chat.py         the agentic loop and the routing system prompt
  claude.py       Anthropic SDK wrapper
  cli.py          prompt_toolkit REPL
  tools.py        namespacing, worker identity, fan-out  ← the reason this exists
```

## Not built yet

- **The router does not expose a `delegate` tool of its own.** Claude Code can
  reach each worker directly but cannot yet drive the whole mesh through one
  endpoint. Adding it means porting ResearchMesh's `mcp_server.py`, which is
  mostly reusable — its hard part, the stdout guard, applies unchanged.
- **No reconnection.** A worker that dies mid-session is skipped with a warning
  on each subsequent turn; it is never retried until you restart.
- **No loop protection.** Nothing stops worker A's config from pointing back at
  this router. Nesting works, but there is no depth counter.
- **No `/workers` command.** The fleet summary prints once per session.
