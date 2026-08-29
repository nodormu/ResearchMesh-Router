# ResearchMesh-Router

> *Unofficial, community-built client — not affiliated with or endorsed by Anthropic. "Claude" is a trademark of Anthropic.*

                                  ┌── Bash / Filesystem
                                  ├── Playwright / LibreOffice
                                  ├── Python kernel / DuckDB
        ResearchMesh-Router ──────┤
                                  ├── workstation ── ResearchMesh (Agent)
                                  ├── gpu-box ────── ResearchMesh (Agent)
                                  └── scraper ────── ResearchMesh (Agent)

The **exact same toolset as [ResearchMesh](https://github.com/nodormu/ResearchMesh)**,
plus the ability to drive any number of ResearchMesh agents running on other
machines — and without the tool-name conflicts that combination normally causes.

Two kinds of tool, in one list:

- **Local** — `bash`, `python`, `computer`, `memory`, `browser_navigate`, … run
  here, immediately.
- **Worker** — `gpu-box__delegate`, `scraper__delegate`, … run on another
  machine, over MCP.

Ask for something and Claude picks the machine. Independent work on different
workers runs at the same time.

**ResearchMesh-Router is NOT an MCP server.** It connects *out* to ResearchMesh
workers, or other MCP servers/agents/etc; nothing connects *in*. That is what
keeps the tool names unambiguous.

**It is a less restrictive orchestrator than Claude Code.** Fewer guardrails: no
approval prompts, no permission model, no context compaction. It runs any
program, command or script your user can run, on this machine and on every
worker, without babysitting. That is the point — and the risk.

## What it can do

**18 local tools**, plus one per connected worker:

| Tool | For |
|---|---|
| `bash` | Shell commands as your user. Stateless — fresh subprocess each call |
| `str_replace_based_edit_tool` | View, create, and edit files |
| `web_search` · `web_fetch` | Anthropic's server-side search and page fetch |
| `memory` | A `/memories` store that **persists across sessions** — the only state that outlives the process |
| `computer` | Screenshots plus mouse/keyboard control. **Needs an X11 session** |
| `browser_navigate` · `_links` · `_click` · `_fill` · `_extract` · `_back` | Headless [Playwright](https://playwright.dev/) — renders JavaScript, follows links, fills forms |
| `document_convert` | LibreOffice + pandoc. Markdown → `.docx`/`.odt`/`.pdf`, or any office format to any other |
| `python` | Persistent IPython kernel — **variables survive between calls** |
| `interactive_run` | Commands that prompt: passwords, `[y/N]`, ssh host keys, installers |
| `config_edit` | Edit YAML/TOML/JSON **without destroying your comments** |
| `sql_query` | DuckDB straight against CSV/Parquet/JSON — no import step |
| `trash` | Recoverable deletes instead of `rm` |
| `<worker>__delegate` | Hand a whole task to a ResearchMesh agent on another machine |

Every machine has its own copy of all this. The `python` kernel here is not a
worker's kernel, and `/memories` here is not a worker's memory store. Same names,
different computers, no shared state.

## Quick start

You need **Linux**, **Python 3.11+**, and an Anthropic **API key** — this is an
API client, so a Claude subscription won't work.

**Workers are optional.** `config.toml` ships with every server commented out, so
a fresh clone runs on the 18 local tools alone.

```bash
sudo apt install python3 python3-venv python3-dev build-essential \
                 libreoffice pandoc python3-tk scrot

python3 -m venv ~/researchmesh-router
source ~/researchmesh-router/bin/activate
pip install -r requirements.txt

playwright install chromium           # pip installs the package, not the browser
sudo playwright install-deps chromium

export ANTHROPIC_API_KEY=sk-ant-...   # add to ~/.bashrc to keep it
export CLAUDE_MEMORY_DIR=~/.router-memories   # else it writes into this repo

python main.py
```

Then just type. At the `>` prompt:

| | |
|---|---|
| `<anything>` | ordinary turn — local tools *and* workers are offered |
| `/workers` | list the workers that are up |
| `/dagent <task>` | delegate-only: the local tools are withheld for this turn |
| `/dagent <worker> <task>` | the same, pinned to one machine |
| `/think <anything>` | give Claude longer to reason |
| `/clear` | drop the conversation, keep the workers connected |

**Ctrl-C** exits and shuts everything down cleanly.

**If it starts returning 400s and won't stop, run `/clear`.** Two failures
persist for the life of the process — an unanswered `tool_use` block, and a
conversation past the context window — and both make every later turn fail
identically. The error report names which one you hit; `/clear` recovers from
either without dropping your worker connections.

**Why `/dagent` exists.** A local `bash` is instant; a `delegate` takes minutes
and has to be written as an outcome. Left alone Claude prefers the local one and
quietly does a worker's job on the wrong machine. `/dagent` removes the local
tools from the request, so it can't.

## Adding workers

Any MCP server works — ResearchMesh is just what it was built and tested against.
Each worker's tools are prefixed with its `name`, so identical machines never
collide.

On each worker machine, run ResearchMesh as a server:

```bash
export RESEARCHMESH_MCP_TOKEN=...
python mcp_server.py --transport streamable-http --host 0.0.0.0 --port 8100
```

Then add it to `config.toml` here:

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

Three fields deserve a second look.

**`name` becomes the tool prefix** — `gpu-box__delegate`. Keep it short; letters,
digits, `_` and `-` only. Anything else is substituted with `_`, so `gpu box` and
`gpu-box` would collide.

**`description` is strongly recommended.** ResearchMesh hardcodes a single
description constant, so every worker describes itself identically unless you
add your own. Write what's true of *that* machine: its OS and session type,
what's installed, what's attached, what data is on it, what it must not be
used for. It's prepended to that worker's tools as `[worker: name] ...` and
makes routing choices much more reliable.

**`timeout_seconds` matters more than it looks.** The MCP SDK defaults to 300s. A
worker driving a GUI runs longer than that, and when the timeout fires the work
is already done on the far side and simply lost. Default here is 900s; raise it
per worker for long compute. The *connect* timeout stays at 15s, so a machine
that's switched off fails in seconds instead of hanging the turn.

**`url` can be `https://`.** The router does not add custom certificate logic; it
uses the normal HTTP client trust configuration for the runtime. A company CA or
private certificate therefore works only if that CA is already trusted on the
client machine, or if `SSL_CERT_FILE=/path/ca.pem` / `SSL_CERT_DIR=/path/to/certs`
is set for that process. The certificate itself belongs on the *worker* side
(`mcp_server.py --ssl-certfile/--ssl-keyfile`). Over plain `http://` the bearer
token and every task and result cross the network in the clear, which is fine on
a trusted LAN and is not on a corporate one.

## How work is distributed

Worker calls are grouped by machine. Groups run concurrently; calls within a
group run in order — because a ResearchMesh worker has one mouse, one browser
page and one kernel, and serialises `delegate` behind a lock. So `max_parallel`
is really "how many machines at once". Local tools run in order for the same
reason.

A turn calling three workers takes as long as the slowest one, not the sum.

## Environment variables

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Read from the shell. The app does not load a `.env`. |
| `CLAUDE_MODEL` | Overrides `[claude] model` in `config.toml`. |
| `CLAUDE_MEMORY_DIR` | Where `memory` stores `/memories`. Defaults to `./memories` **relative to the working directory** — set it. |
| `CLAUDE_SHOW_USAGE=1` | Per-request token and prompt-cache counters. |
| `CLAUDE_KERNEL_ENCRYPTION` | `auto` (default) encrypts the local `python` kernel's ZeroMQ sockets with CurveZMQ, falling back if the installed versions can't; `required` fails the tool rather than running unencrypted; `off` skips it. Covers *this* machine's kernel only — a worker's kernel reads the variable from the worker's own environment. |
| *(per worker)* | Each `token_env` names the variable holding that worker's bearer token. No `token_env` means unauthenticated. |

Tokens are never stored in `config.toml`, which is committed. Generate one with
`python -c "import secrets; print(secrets.token_urlsafe(32))"`.

## Checks

```bash
ruff check .        # should be clean
mypy .              # should be clean
python smoke_test.py
```

`smoke_test.py` needs no API key, no network and no running workers — it builds a
fleet of fakes and asserts the things that break *silently*: that two workers
exposing the same tool name get two distinct, API-legal names; that the
namespacing round-trips; that a dead worker is skipped instead of taking the
fleet down; that groups fan out while one worker's calls stay serial; that every
`tool_use` block gets exactly one `tool_result`, in order; and that `/dagent`
really withholds every local schema.

There's a fourth check, kept out of the gates because it spends real tokens:

```bash
python e2e_test.py     # ~15s, needs ANTHROPIC_API_KEY
```

It launches two workers over real stdio MCP and covers what fakes can't — that
the duplicate-name 400 is genuinely the API's behaviour, that namespacing
survives a real transport, and that a real model issues both calls in one turn.

## Project layout

```
main.py           entrypoint — loads config, connects the fleet, runs the REPL
mcp_client.py     MCP client (stdio / SSE / Streamable HTTP)
config.toml       the fleet, and router behaviour
smoke_test.py     the offline gate
e2e_test.py       live check against real workers (costs tokens, not a gate)
core/
  chat.py         the agentic loop, routing prompt, /dagent
  claude.py       Anthropic SDK wrapper
  cli.py          prompt_toolkit REPL
  tools.py        namespacing, worker identity, fan-out  ← the reason this exists
  local_tools.py  registry — the one place a local tool is wired in
  browser.py  computer.py  kernel.py  memory.py  data.py  documents.py
  processes.py  config_edit.py  files.py  output.py  claude_learned_schemas.py
```

Adding a **worker** is a config edit, no code. Adding a **local tool** is one
module exposing `TOOLS` / `handles()` / `execute()`, plus a line in
`local_tools.py`.

## Origin

The CLI shell, Anthropic wrapper and MCP client began as copies from
[ResearchMesh](https://github.com/nodormu/ResearchMesh) (same author, MIT); the
twelve tool modules were copied later, verbatim. `diff -rq ../ResearchMesh/core
core` should show only `chat.py`, `claude.py`, `tools.py` and `cli.py` — anything
else is drift. A fix to a tool in either repo should be a straight `cp`.

It exists because a plain MCP bridge passes tool names through verbatim, so three
ResearchMesh workers all advertising `delegate` get rejected outright
(`400 ... Tool names must be unique`). [core/tools.py](core/tools.py) is the fix.
**If Claude Code is your front end you don't need any of this** — it already
namespaces MCP tools as `mcp__<server>__<tool>`.

## Not built yet

- **No `mcp_server.py`.** Claude Code can reach each worker directly but can't
  drive the whole mesh through one endpoint, and this can't be a worker in
  someone else's fleet.
- **No reconnection.** A worker that dies mid-session is skipped each turn until
  you restart.
- **No loop protection.** Nothing stops a worker's config pointing back here.
- **`/workers` lists only what's up.** A down worker is absent, not shown as
  down — so a rejected name could be a typo or a switched-off machine.
- **No approval gating, on two machines.** The router executes locally *and*
  sends whatever it decides to any worker, which executes without asking.
