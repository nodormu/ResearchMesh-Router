# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

ResearchMesh-Router is a command-line agent that routes work across a fleet of
MCP worker agents on other machines, running independent work concurrently — and
that **also holds a full local toolset of its own**, merged in from ResearchMesh.

> **This overview changed.** The router originally owned no tools and executed
> nothing; that was its defining property, and much of what follows was written
> to explain it. The local tools in `core/` were merged back in deliberately,
> so where a comment or a doc still says "the router executes nothing", the
> comment is stale, not the code. What survived the merge unchanged is the part
> that matters: worker tools are namespaced, and a worker's schemas never reach
> this process's API request.

Two halves, in one `tools` array on every request:

- **Local tools** — plain names (`bash`, `python`, `computer`, `memory`,
  `browser_*`, `sql_query`, …), executed in-process on the router's own machine.
- **Worker tools** — `<worker>__<tool>`, forwarded over MCP to another machine.

They cannot collide: a ResearchMesh worker advertises exactly one tool over MCP
(`delegate`), and every worker tool is namespaced with a `__` separator that no
local name contains. `ToolManager.build` also takes a `reserved` set of the local
names so the two halves are checked for uniqueness against each other rather than
each in isolation — uniqueness is a property of the whole array, and a duplicate
400s the entire request, not just the offending tool.

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
| `core/cli.py` | copied; diverged when `/workers` and `/dagent` were added |
| `mcp_client.py` | copied, plus `timeout_seconds` |
| `core/browser.py`, `computer.py`, `config_edit.py`, `data.py`, `documents.py`, `files.py`, `kernel.py`, `memory.py`, `processes.py`, `claude_learned_schemas.py`, `local_tools.py`, `output.py` | copied verbatim in the tool merge, unchanged |
| `main.py`, `core/chat.py` | same skeleton; local-tool wiring restored, `SYSTEM_PROMPT` rewritten |
| `core/claude.py` | same, including the beta endpoint (restored with `computer`) |
| `core/tools.py` | rebuilt for this project; only the result-formatting helpers survive |
| `smoke_test.py`, `e2e_test.py`, `e2e_worker.py`, `config.toml` | written here |

The twelve tool modules are byte-identical copies. Keep them that way — a fix in
either repo should be a straight `cp`. `diff -rq ../ResearchMesh/core core`
currently reports exactly three differing files (`chat.py`, `claude.py`,
`tools.py`); `cli.py` and all twelve tool modules match byte for byte, and
anything else appearing in that list is drift worth explaining.

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

Request flow: **CLI input → Chat.run() agentic loop → Claude API + local tools +
worker MCP tools**.

- **`main.py`** — entrypoint. Reads config, connects each `[mcp].servers` entry
  (a worker that fails is reported and skipped, never fatal), builds the
  worker-description map, and wires a `Chat` into the `CliApp` loop. Like
  ResearchMesh's, it registers `local_tools.shutdown` on the `AsyncExitStack`
  alongside each worker's `cleanup` — the router now has a browser, an IPython
  kernel and a DuckDB connection of its own to release.

- **`core/tools.py`** — **the file that differs substantially from ResearchMesh's,
  and the reason this repo exists.** Three changes over that bridge:
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

- **`core/chat.py`** — the agentic loop, structurally the same as ResearchMesh's:
  `_run_tool_uses` tries each block against `local_tools.execute` first and falls
  through to `ToolManager.execute_blocks` when no local module owns the name.
  `_local_result_to_content` is back with it, translating the
  `core.output.image_result` marker into a real `image` block (worker images take
  the parallel path through `_call_one`).

  Two things here are *not* copies of ResearchMesh. **Results are reassembled in
  the original block order** via a `by_id` dict — splitting a turn across two
  executors and appending the halves would otherwise reorder every mixed turn,
  and `execute_blocks` promises order for the worker half. And **local tools are
  declared first** in `tool_defs`: tools render ahead of `system` in the cached
  prefix, so putting the static half first keeps the front of the prefix stable
  when a worker drops out mid-session.

  `SYSTEM_PROMPT` is a **full rewrite, not an edit**, and has now been rewritten
  twice. It used to open with "you have no tools of your own", which the merge
  made false. It now leads with the local/remote split — plain name means this
  machine, `<worker>__` means another — because nothing in a tool definition says
  which, and the model otherwise has no way to tell `python` here from a worker's
  kernel. The rest states the topology: workers share no filesystem or state with
  each other *or with this machine*, `delegate` wants an outcome rather than a
  command, how `session` ids work and that a fresh one does not clear
  `/memories`, that batching across workers parallelises but batching within one
  does not, and that delegations are slow and must not be polled. It also has to
  push *against* the local tools, which are faster and more directly matched to
  any concrete command the model has in mind — the failure mode after the merge
  is under-delegation, doing a worker's job on the wrong machine. **Keep it
  factual and update it if the routing model changes.**

- **`core/claude.py`** — thin Anthropic SDK wrapper, now the same as
  ResearchMesh's. It posts to `client.beta.messages.create` with
  `betas=[computer-use-2025-11-24]` because `local_tools` declares the
  beta-gated `computer` tool on every request, and omitting the header 400s the
  whole conversation rather than just computer use. That endpoint returns
  `BetaMessage`, which is **not** a subclass of `Message`, so the isinstance
  checks need the `_RESPONSE_TYPES` tuple covering both — without it they
  silently stuff the response object into `content` instead of its blocks. This
  is the subtlest trap in the file; the tool merge is what brought it back.
  Top-level `cache_control` works on both endpoints, so prompt caching is
  unaffected either way. **A worker's beta-gated tools remain entirely its own
  problem** — it makes its own API call with its own headers, and nothing about
  a worker's schemas reaches this request. That is still the property that makes
  the design work, and it is why the router declaring `computer` locally has no
  bearing on any worker that also has one.

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

- **`core/cli.py`** — was verbatim from ResearchMesh; now carries the two
  router-specific commands, which is the only reason it diverged.

  - **`/workers`** — the fleet that is **up**, with the exact names `/dagent`
    takes. Answered locally without spending a turn. It rebuilds the index
    rather than caching one, because a cached listing would happily report a
    machine that went down ten minutes ago. Workers that failed to connect, or
    have died since, are simply absent — that is deliberate: absent and
    unreachable are the same thing from the model's point of view.
  - **`/dagent [worker] <task>`** — one turn with the local tools **withheld**,
    optionally pinned to a single machine. The enforcement is the point: it
    filters `tools`, it does not instruct the model. A local `bash` is faster
    and more directly matched to any concrete command than a `delegate` that
    takes minutes, so an instruction is a preference and an absent schema is a
    fact. `_DELEGATE_ONLY_SUFFIX` is appended to the system prompt as well, only
    because `SYSTEM_PROMPT` still describes local tools by name and the model
    would otherwise spend the turn wondering where `bash` went.

    Two costs, both accepted. It **misses the prompt cache in each direction** —
    tools render ahead of `system`, so changing the list invalidates everything
    after it on the `/dagent` turn and again on the next ordinary one. And it
    **aborts rather than sending an empty tool list**: withholding the locals
    when no worker tools exist would leave a turn with no tools at all, which
    the model answers from thin air — the exact outcome the command exists to
    rule out. That path pops the user message back off `self.messages` so an
    aborted turn leaves no trace.

## Runtime configuration

- `ANTHROPIC_API_KEY` — from the shell. `main.py` keeps an explicit `os.getenv`
  reference for the same reason ResearchMesh does; do not remove it.
- `CLAUDE_MODEL` — overrides `[claude] model`.
- `CLAUDE_SHOW_USAGE=1` — per-request token and cache counters. Worth more here
  than in ResearchMesh: the tool list is built from *live* workers, so a worker
  dropping out mid-session reshapes the cached prefix and silently costs the hit.
  Declaring the local tools first limits the damage but does not remove it.
- `CLAUDE_MEMORY_DIR` — where the local `memory` tool's virtual `/memories` tree
  actually lives. **Set it explicitly.** It defaults to `./memories` relative to
  the working directory, so left alone the router grows its own store in this
  repo. A ResearchMesh worker launched over stdio is safe from colliding with it
  (`mcp_server.py` chdirs to its own root before anything reads the variable),
  but a non-ResearchMesh stdio server with relative-path state is not — `main.py`
  passes no `cwd`, so such a subprocess inherits the router's.
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
7. the local half of the list is API-legal and cannot collide with a namespaced
   worker name (the `reserved` set), and a turn **mixing** local and worker calls
   still returns one result per block in the original order. That last one is the
   merge's own regression surface: `_run_tool_uses` now assembles results from
   two executors, and the obvious implementation — locals in a list, workers
   appended after — silently reorders every mixed turn.

The local checks execute `bash` with no `command`, which returns an error string
without running anything, so the file stays free of network calls and side
effects.

## Conventions carried over from ResearchMesh

- **Blanket `except` is deliberate.** Rarer here (no local tool can crash the
  loop) but the remaining sites are load-bearing for the same reason: a worker on
  another machine can fail in any way, and every `tool_use` owes a `tool_result`.
  Don't narrow them.
- **Cleanup paths must not fail, and must not fail silently** — blanket catch
  *plus* a `print()`. When `S110` fires, the defect it names is the silence.
- **The app must run from the repo root.** Sharper since the merge: `memory`'s
  `CLAUDE_MEMORY_DIR` defaults to a *relative* `./memories`, so the cwd decides
  whose memory store you get.
- **No approval gating.** The router now executes locally *and* sends whatever it
  decides to any worker, and each worker executes without approval either. Two
  machines' worth of unapproved execution from one prompt.

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
