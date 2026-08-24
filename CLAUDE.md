# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

ResearchMesh-Router is a command-line agent that **owns no tools and executes
nothing** — no bash, no editor, no browser, no kernel, no screenshots, no memory
store. Every capability it has belongs to an MCP server on another machine. Its
only job is choosing which machine does what, and running independent work
concurrently.

It is built for fleets of **interchangeable** workers: several machines running
the same agent software, differing only in what is installed, attached, or
stored on them. Nothing in it is specific to any one worker implementation —
[ResearchMesh](https://github.com/nodormu/ResearchMesh) is the worker it was
built and tested against, not a dependency.

**This file is self-contained.** Nothing in this codebase requires reading
another project's docs. ResearchMesh's own CLAUDE.md is still the place to look
when a *worker's* behaviour is the question — what `delegate` does with a task
once it arrives, what its local tools are — but that is about the far end of the
wire, not this code.

## Origin

A standalone project with its own history. It did not start from nothing,
though: the CLI shell, the Anthropic wrapper and the MCP client began as copies
from ResearchMesh (same author, MIT), and knowing which parts are inherited
tells you where to be careful.

| File | Where it came from |
|---|---|
| `core/cli.py` | copied, still unchanged |
| `mcp_client.py` | copied, plus `timeout_seconds` |
| `main.py`, `core/chat.py` | same skeleton, local-tool wiring removed; `SYSTEM_PROMPT` rewritten |
| `core/claude.py` | same, minus the beta endpoint |
| `core/tools.py` | rebuilt for this project; only the result-formatting helpers survive |
| `smoke_test.py`, `e2e_test.py`, `e2e_worker.py`, `config.toml` | written here |

**One thing to know for the next MCP SDK major.** `mcp_client.py` and
`core/tools.py` are the two files that broke on mcp 1.x → 2.x: the transport
function was renamed, headers moved onto an httpx2 client built by
`create_mcp_http_client`, `read_timeout_seconds` went from `timedelta` to
`float`, and the model fields went snake_case. That is history — the code here
inherited the already-fixed versions — but it identifies where an mcp 3.0 would
land, and it would land in ResearchMesh too.

If that happens, the two are still trivially comparable despite the separate
histories: `core/cli.py` is byte-identical to its counterpart, and
`mcp_client.py` diverges by about nine lines of real code (the `timeout_seconds`
parameter and its two uses). `diff -u ../ResearchMesh/mcp_client.py
mcp_client.py` shows the whole of it. Shared ancestry would only have added
`git cherry-pick` as a convenience.

`mypy .` is what will tell you a break has happened at all, since it checks
against the *installed* packages rather than a pinned version.

## Commands

Run the app **from the repo root**:

```bash
python main.py
```

Requires `ANTHROPIC_API_KEY` in the shell (no `.env` is loaded), plus one
environment variable per authenticated worker, named by that worker's
`token_env` in `config.toml`.

Check every configured worker standalone (connect, list tools, report failures):

```bash
python mcp_client.py
```

Three gates, all of which should come back clean, same bar as ResearchMesh:

```bash
ruff check .
mypy .
python smoke_test.py
```

`smoke_test.py` needs no API key, no network and no running workers — it builds a
fleet of fakes. It is *not* a port of ResearchMesh's smoke test: there is no tool
registry to validate and no `mcp_server.py` to handshake with, so it checks a
different set of invariants (see "What the smoke test guards" below).

A fourth check exists but is **not** one of the gates, because it spends real
tokens:

```bash
python e2e_test.py     # ~15s, needs ANTHROPIC_API_KEY
```

It launches two `e2e_worker.py` subprocesses over real stdio MCP and covers the
three things fakes cannot: that the duplicate-name 400 is genuinely the API's
behaviour and not folklore in a comment, that namespacing survives a real
transport, and that a real model — given only the `[worker: …]` description
headers — actually issues both calls in one turn. That last one is the subtle
one: plumbing that fans out perfectly but which the model never triggers would
pass every check in `smoke_test.py`. Run it after touching `core/tools.py`,
`mcp_client.py`, or `SYSTEM_PROMPT`.

## Why this repo exists

A ResearchMesh instance served as `mcp_server.py` exposes exactly one tool,
`delegate`. Three workers therefore advertise three tools with the same name, and
the API rejects the whole request:

```
400 invalid_request_error: tools: Tool names must be unique.
```

That is a verified response, not an inference. ResearchMesh's `ToolManager`
compounds it: `get_all_tools` copies `t.name` verbatim, and `_tool_owners` uses
`setdefault`, so even without the 400 only the first worker would ever be
reachable — silently, since nothing reports the loser.

**Claude Code is unaffected** and needs none of this: it namespaces MCP tools as
`mcp__<server>__<tool>` already. This repo exists for the case where the *boss*
is itself a ResearchMesh-shaped CLI.

## Architecture

Request flow: **CLI input → Chat.run() agentic loop → Claude API + worker MCP tools**.

- **`main.py`** — entrypoint. Reads config, connects each `[mcp].servers` entry
  (a worker that fails is reported and skipped, never fatal), builds the
  worker-description map, and wires a `Chat` into the `CliApp` loop. Unlike
  ResearchMesh's, it registers no local shutdown callback — there is no browser,
  kernel or DuckDB connection to release; each worker's `cleanup` is on the same
  `AsyncExitStack`.

- **`core/tools.py`** — **the only file that differs substantially, and the
  reason this repo exists.** Three changes over ResearchMesh's bridge:
  1. *Namespacing.* Tools are declared as `<worker>__<tool>` and the prefix is
     stripped before the call goes out; the worker never learns it was renamed.
     `_legalise()` enforces the API's `^[a-zA-Z0-9_-]{1,128}$` rule, substituting
     illegal characters, truncating with a **deterministic** hash suffix (a name
     that changed between requests would silently cost the prompt cache every
     turn), and disambiguating collisions.
  2. *Worker identity.* Each tool's description gets a `[worker: name] <blurb>`
     header from the config entry's `description` field. This is not cosmetic —
     without it, namespacing produces distinctly-named tools carrying
     byte-identical text, because ResearchMesh hardcodes one
     `_DELEGATE_DESCRIPTION` constant. Config-side rather than worker-side so it
     needs no coordinated redeploy and works against non-ResearchMesh servers.
  3. *Fan-out.* `execute_blocks` groups blocks by owning worker and runs the
     **groups** concurrently, sequential within a group. That mirrors the far
     end: a ResearchMesh worker serialises `delegate` behind an `asyncio.Lock`
     because it has one mouse, one browser page and one kernel, so two
     simultaneous calls to one worker gain nothing and risk the second aging out
     on that lock.

  It also takes a `Worker` **Protocol** rather than a concrete `MCPClient`. The
  bridge genuinely only needs `list_tools` and `call_tool`, and the protocol is
  what lets `smoke_test.py` exercise namespacing and fan-out against fakes.
  Note `Mapping[str, Worker]`, not `dict`: dict is invariant in its value type,
  so `dict[str, MCPClient]` would not satisfy `dict[str, Worker]`.

  One structural change falls out of the above: `ToolIndex` is built **once per
  user turn** by `Chat` and passed into `execute_blocks`, rather than each
  execution pass re-deriving owners. ResearchMesh's version costs a `list_tools`
  round trip per client per tool-use iteration — invisible in-process, but a
  network round trip per worker up to `MAX_TOOL_ITERATIONS` times per question
  here.

- **`core/chat.py`** — the agentic loop, structurally the same as ResearchMesh's
  minus the local-tool branch (and therefore minus `_local_result_to_content`;
  worker images arrive through `execute_blocks` instead). `SYSTEM_PROMPT` is a
  **full rewrite, not an edit**. ResearchMesh's exists to stop Claude inventing
  local capabilities it lacks; this one has the opposite problem — every
  capability is real but lives on a different machine, and nothing in a tool
  schema conveys that. Left to the schemas alone the model treats the fleet as
  one computer: reads a path from one worker and writes it on another. The prompt
  states the topology, that workers share no filesystem or state, that `delegate`
  wants an outcome rather than a command, how `session` ids work, that batching
  across workers parallelises but batching within one does not, and that
  delegations are slow and must not be polled. **Keep it factual and update it if
  the routing model changes.**

- **`core/claude.py`** — thin Anthropic SDK wrapper, and **simpler than
  ResearchMesh's on purpose**. That version must post to
  `client.beta.messages.create` with `betas=[computer_20251124]` because it
  declares the `computer` tool on every request and omitting the header 400s the
  whole conversation. This client declares no tools of its own, so nothing is
  beta-gated and `client.messages.create` is correct. Dropping the beta endpoint
  also deletes the subtlest trap in the original: it returns `BetaMessage`, which
  is *not* a subclass of `Message`, so the isinstance checks needed a
  `_RESPONSE_TYPES` tuple or they would silently stuff the response object into
  `content`. Top-level `cache_control` is available on the stable endpoint too
  (checked against the installed SDK, not assumed), so prompt caching is
  unaffected. **A worker's beta-gated tools are entirely its own problem** — it
  makes its own API call with its own headers; nothing about a worker's schemas
  reaches this request. That is the property that makes the whole design work.

  Four constraints on `chat()`, each learned from a 400 and recorded here so
  this file stands alone:
  - **No sampling parameters.** Current models (Sonnet 5, Opus 5, Opus 4.7+)
    reject a non-default `temperature`/`top_p`/`top_k` outright, and accept only
    the default — so sending one can never do anything but fail. Steer with
    `SYSTEM_PROMPT` instead.
  - **No `budget_tokens`.** Adaptive thinking replaced it; the 4.5-era
    `{"type": "enabled", "budget_tokens": N}` is now a 400. The depth knob, if
    ever needed, is `output_config={"effort": …}`.
  - **`max_tokens=8000` is shared by thinking and the reply**, so a `/think`
    turn on a hard problem can end on `stop_reason: "max_tokens"`. Raise it if
    that bites; streaming becomes advisable much above ~16K.
  - **Prompt caching fails silently.** A prefix under the model's minimum
    (1024 tokens on Sonnet 5) simply isn't cached, with no error, and any byte
    change early in the prefix invalidates everything after it.
    `CLAUDE_SHOW_USAGE=1` is the only way to confirm it is landing.

- **`mcp_client.py`** — kept close to the copy it came from, deliberately, so
  the two remain easy to compare by eye when the MCP SDK next changes shape.
  Resist tidying it for its own sake. **One deliberate divergence:
  `timeout_seconds`.** Both defaults are too short for a worker that runs a whole
  agentic loop before replying — `create_mcp_http_client` defaults to a 300s read
  timeout — and when it fires the work is already done on the far side and lost.
  It is applied in two independent places: the httpx read timeout, and
  `ClientSession(read_timeout_seconds=…)`, which is a plain float in mcp 2.x and
  was a `timedelta` in 1.x. Connect stays at 15s deliberately, so an
  switched-off machine fails fast instead of hanging the turn.

- **`core/cli.py`** — verbatim from ResearchMesh.

## Runtime configuration

- `ANTHROPIC_API_KEY` — from the shell. `main.py` keeps an explicit `os.getenv`
  reference for the same reason ResearchMesh does; do not remove it.
- `CLAUDE_MODEL` — overrides `[claude] model`.
- `CLAUDE_SHOW_USAGE=1` — per-request token and cache counters. Worth more here
  than in ResearchMesh: the tool list is built from *live* workers, so a worker
  dropping out mid-session reshapes the cached prefix and silently costs the hit.
- `[router] max_parallel` (default 8) — how many workers may be busy at once.
- `[router] timeout_seconds` (default 900) — per-call deadline; override per
  worker with `timeout_seconds` on its entry.
- **Worker bearer tokens** — each entry's `token_env` names the variable holding
  its token, sent as `Authorization: Bearer <token>`. No `token_env` means
  unauthenticated, by design. This is the consuming half of the contract
  `mcp_server.py --token-env` implements on the serving side; both ends normally
  read the *same* variable name, and a second name is only needed on a machine
  that both serves and consumes.
- **`$VAR` in `[mcp].servers`** — `tomllib` does no substitution, so
  `_expand_paths()` expands `~` and `$VAR`/`${VAR}` in `command`, `url` and the
  *values* of `env`. Keys of `env` are variable names and are left alone, and
  **`description` is deliberately not expanded** — it is prose for the model, so
  a `$` in it is a dollar sign.
- Python 3.11+ — the floor is `tomllib`.

## What the smoke test guards

Not a port of ResearchMesh's. There is no local tool registry and no
`mcp_server.py`, so the checks are:

1. everything imports and byte-compiles;
2. two workers exposing the same tool name yield two distinct, API-legal names —
   **the regression this whole repo prevents**, and one that nothing else would
   catch until a second worker was connected and a real turn attempted;
3. the namespacing round-trips (the worker is called with its own bare name);
4. a worker that fails `list_tools` is skipped without taking the fleet down;
5. execution fans out across workers but stays serial within one, and
   `max_parallel` actually bounds it. **This one matters more than it looks**: a
   refactor that quietly reverted `execute_blocks` to a sequential loop would
   still return correct answers and pass every other check — just three times
   slower, invisibly;
6. every `tool_use` block gets exactly one `tool_result`, in the original order,
   including for an unknown tool. An unanswered block poisons every later request
   in the session with a 400 about unresolved ids.

## Conventions carried over from ResearchMesh

- **Blanket `except` is deliberate.** Rarer here (no local tool can crash the
  loop) but the remaining sites are load-bearing for the same reason: a worker on
  another machine can fail in any way, and every `tool_use` owes a `tool_result`.
  Don't narrow them.
- **Cleanup paths must not fail, and must not fail silently** — blanket catch
  *plus* a `print()`. When `S110` fires, the defect it names is the silence.
- **The app must run from the repo root.**
- **No approval gating.** The router itself runs nothing, but it will send
  whatever it decides to any worker, and each worker executes without approval.

## Adding a worker

A config edit, not a code change: add an entry to `[mcp].servers` with a `name`,
a `url` or `command`, and — the part that is easy to skip and matters most — a
`description` that would actually change a routing decision. Nothing else in the
codebase enumerates workers, so unlike adding a *tool* in ResearchMesh there is
no ten-place checklist here.

## Deliberately not built

- The router exposes no `delegate` tool of its own, so it cannot yet be driven as
  a single endpoint by Claude Code or by another router. Porting ResearchMesh's
  `mcp_server.py` would mostly work; its hard part (the file-descriptor-level
  stdout guard) applies unchanged.
- No reconnection: a worker that dies is skipped each turn until restart.
- No recursion or loop protection for a worker configured to point back here.
