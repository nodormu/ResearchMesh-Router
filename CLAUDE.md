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
| `core/cli.py` | copied; diverged when `/workers`/`/dagent` were added, and again when `/voice`/`/listen` were ported over from ResearchMesh |
| `mcp_client.py` | copied, plus `timeout_seconds` |
| `core/browser.py`, `computer.py`, `config_edit.py`, `data.py`, `documents.py`, `files.py`, `kernel.py`, `listen.py`, `memory.py`, `processes.py`, `speak.py`, `claude_learned_schemas.py`, `local_tools.py`, `output.py`, `text_embeddings.py`, `vision.py` | copied verbatim in the tool merge, unchanged |
| `core/bash_session.py`, `process_reaper.py` | ported from ResearchMesh after the tool merge (bash_session added there first, process_reaper alongside it), copied verbatim, kept in the same byte-identical set as the row above |
| `main.py`, `core/chat.py` | same skeleton; local-tool wiring restored, `SYSTEM_PROMPT` rewritten |
| `core/claude.py` | same, including the beta endpoint (restored with `computer`) |
| `core/tools.py` | rebuilt for this project; only the result-formatting helpers survive |
| `smoke_test.py`, `e2e_test.py`, `e2e_worker.py`, `config.toml` | written here |

The eighteen tool modules are byte-identical copies. Keep them that way — a fix in
either repo should be a straight `cp`. `diff -rq --exclude=__pycache__
../ResearchMesh/core core` currently reports five differing files (`chat.py`,
`claude.py`, `cli.py`, `local_tools.py`, `tools.py`) plus one file only on the
ResearchMesh side (`midi1.py` — this repo has no MIDI tool); all eighteen tool
modules match byte for byte, and anything else appearing in that list is drift
worth explaining. **`cli.py` is expected to differ, not a regression** — see its
own Architecture bullet below for exactly what it carries beyond ResearchMesh's
copy (`/workers`/`/dagent`, genuinely router-specific; `/voice`/`/listen`, ported
over and behaviorally identical to ResearchMesh's own). **`local_tools.py` is
expected to differ too** — its `MODULES` list correctly has no `midi1` entry,
since this repo carries no MIDI tool; re-run the `diff` above rather than
trusting this file count if the tool set on either side ever changes.

**One thing to know for the next MCP SDK major.** `mcp_client.py` and
`core/tools.py` are the two files that broke on mcp 1.x → 2.x: the transport
function was renamed, headers moved onto an httpx2 client built by
`create_mcp_http_client`, `read_timeout_seconds` went from `timedelta` to
`float`, and the model fields went snake_case. That is history — the code here
inherited the already-fixed versions — but it identifies where an mcp 3.0 would
land, and it would land in ResearchMesh too.

If that happens, the two are still trivially comparable despite the separate
histories: the eighteen tool modules (see the table above) stay byte-identical
copies by convention, and `mcp_client.py` diverges by about nine lines of real
code (the `timeout_seconds` parameter and its two uses). `diff -u
../ResearchMesh/mcp_client.py mcp_client.py` shows the whole of it. `core/cli.py`
is the one file that's *expected* to diverge (router-specific commands layered
on top of a ported base — see its own Architecture bullet), so it is not part of
this "should be comparable" set. Shared ancestry would only have added
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

```bash
pip install -r requirements.txt
playwright install chromium   # pip installs the package, not the browser itself
```

**Full install walkthrough (apt packages, the `computer` tool's X11/Wayland
requirement, LibreOffice/Pandoc) lives in `README.md`'s "Setup (Linux)"
section.** Don't re-derive that walkthrough here — the two commands above are
what get a working dev environment; the rest there is one-time OS-level setup.
This repo needs the exact same backings as ResearchMesh's own local tools,
since the merge brought them in verbatim (see Origin above).

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
fleet of fakes. It is *not* a port of ResearchMesh's smoke test: there is no local
tool registry to validate here, so it checks a different set of invariants (see
"What the smoke test guards" below).

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

- **`core/cli.py`** — was verbatim from ResearchMesh; now carries two kinds of
  addition. `/workers` and `/dagent` are genuinely router-specific — the only
  actual divergence in *behavior* from ResearchMesh's own `cli.py`. `/voice`,
  `/listen`, and `/model`, by contrast, are straight ports. `/voice`/`/listen`
  reuse `core/speak.py`/`core/listen.py` (byte-identical, per the table
  above), same `_submit()` refactor, same auto-submit-on-dictation design —
  the only change from ResearchMesh's version is threading
  `remote_only`/`worker` through `_submit()` so a dictated turn still
  respects whatever `/dagent` state, if any, was in effect (in practice:
  never, since a dictated turn is always a plain new turn — you can't speak a
  `/dagent` prefix and a task in the same breath). See ResearchMesh's own
  `speak_listen_tool_integration_plan.md` (in *its* `/memories`, not this
  repo's) for the full design history and live-test log behind
  `/voice`/`/listen`; nothing here differs from what's documented there.
  `/model`/`/model swap` reuse `core/claude.py`'s `load_claude_models()`/
  `resolve_model_swap()` (also ported, see the Runtime configuration section
  below) — the branch itself is byte-identical logic to ResearchMesh's,
  differing only in where it sits relative to `/dagent`'s worker-prefix
  parsing lower down in the same `run()` loop. See ResearchMesh's own
  `adding-model-command-to-swap-between-Anthropic-models.md` (in *its*
  `/memories`) for the full design history, including the live-scan/TTL/cache
  work behind `refresh_claude_models()`. (`/clear` was added to both repos
  independently and is not a divergence either.)

  - **`/clear`** (also `/reset`) — empties `self.messages`, keeps the fleet
    connected. This is the recovery path from the two failures that *persist*:
    an unanswered `tool_use` block, which stays in the history for the life of
    the process and fails every later request, and a conversation past the
    context window. Before it existed the only way out was killing the router,
    which also drops every worker connection and every worker-side `session` id.
    `Chat._report_api_failure` prints which of the two you hit — it checks for
    orphaned `tool_use` ids directly rather than guessing from the error text.
    Note the size report is in **characters, not tokens**: `count_tokens`
    cannot measure this conversation at all, because `web_search`/`web_fetch`
    are server tools and that endpoint rejects them.

  - **`/model`** (bare) lists `config.toml`'s `[claude] claude_models` array
    with 1-based indices and marks whichever one
    `self.agent.claude_service.model` currently is; **`/model swap <name or
    index>`** mutates that same attribute directly (`Claude.chat()` reads
    `self.model` fresh every call, so this takes effect on the very next
    turn, no restart) — session-only, it never writes `config.toml`, so a new
    session always starts on `claude_models[0]`. Affects **only the router's
    own reasoning model**; a connected worker's model is entirely its own
    concern, unaffected by this command. An unrecognized name/index or a bare
    `/model swap` with no argument rejects with a message and does not
    swap — same reject-don't-crash posture as `/voice`/`/listen`.

  - **`/model <worker>`** / **`/model <worker> swap <name or index>`** — the
    Phase-R3 counterpart of the bare command above: reaches into a CONNECTED
    worker instead of the router itself. Checked *before* the bare-`/model`
    parsing (`sub in self.agent.clients`), so `/model gpu-box` is recognized
    as worker-targeted rather than an unrecognized router subcommand named
    `gpu-box`. Calls that worker's own `model` MCP tool directly via
    `self.agent.clients[worker_id].call_tool("model", ...)` — bypassing
    Claude and `ToolManager` entirely, the same free/local/no-API-call
    pattern `/workers` above already uses. Deliberately NOT built on
    `Chat.split_worker()`: that helper requires a non-empty remainder (it
    exists for `/dagent`, which always needs a task after the worker name),
    but a bare `/model <worker>` legitimately has nothing after the worker
    name at all — this command parses the worker prefix itself for exactly
    that reason. The precedence check and its arg-parsing live in
    `Chat.resolve_worker_model_request()` (sync, pure), and the actual
    fallible call plus response formatting in `Chat.call_worker_model()`
    (async) — both extracted out of `core/cli.py`'s dispatch branch
    specifically so this cross-process contract is unit-testable without a
    live REPL or a real worker subprocess (`smoke_test.py`'s
    `check_model_worker_dispatch()`, 18 assertions, including the
    worker-literally-named-"swap" precedence case). `core/cli.py`'s own
    branch is now just the thin print/continue wrapper every other command
    here already is. Every line printed for a worker result is prefixed
    `[worker: <name>] ` (same tag format `core/tools.py` already uses for a
    worker's namespaced tool descriptions) — a deliberate, confirmed-with-
    the-user design choice so a remote result can never read like the
    router's own bare `/model` output; the two are never allowed to look the
    same. No TTL/cache logic of any kind lives on the router's side of this
    command — a worker's own `model` tool owns its own live-scan/TTL
    decision entirely (see ResearchMesh's own `core/claude.py`), the same
    way the router owns that decision for its own model above. **This
    command is optional, not required** — because a connected worker's
    `model` tool is merged/namespaced into the tool list exactly like
    `delegate` is (`core/tools.py`'s existing namespacing, unmodified),
    the router's own Claude can discover and call it during an ordinary
    turn given a plain-language ask (e.g. "swap gpu-box to opus") with
    no slash command at all — confirmed live, a real behavior and not just
    a theoretical consequence of the namespacing code. See
    `adding-model-command-to-swap-between-Anthropic-models.md` (in
    ResearchMesh's own `/memories`) for the full design history of both
    halves of this feature.

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

  - **`/voice [on|off]`** — toggles `self.auto_speak` (default off), which
    gates only whether MY reply also gets spoken aloud via `speak.py`'s own
    `_run` helper after a turn completes. It has zero bearing on whether
    `speak`/`listen` are reachable as Claude-invoked tools at all (that's
    `config.toml`'s own `[speak].enabled`/`[listen].enabled`), and zero bearing
    on whether a `/listen` dictation gets submitted — those two concerns are
    deliberately decoupled.
  - **`/listen [N]`** — records `N` seconds from the configured mic (or
    `[listen].default_duration_seconds` if omitted), transcribes locally via
    `listen.py`'s own `_run` (faster-whisper), then **auto-submits the
    transcript as a turn the instant transcription completes** — via the same
    shared `_submit()` method a normal typed Enter-submit uses, so this fires
    identically whether `/voice` is on or off. This is a deliberate pivot away
    from an earlier "stage the transcript as the next prompt's editable
    pre-fill, review before pressing Enter" design (ResearchMesh's own history,
    inherited here unchanged) — a garbled transcript now gets sent as-is, with
    no edit step, a known and accepted tradeoff. A bad `/listen abc` (non-integer
    duration) reports an error and submits nothing; `[listen].enabled = false`
    (or unset `device`) reports `disabled`/`not_configured` and never opens the
    microphone.

## Runtime configuration

- `ANTHROPIC_API_KEY` — from the shell. `main.py` keeps an explicit `os.getenv`
  reference for the same reason ResearchMesh does; do not remove it.
- **Claude Code CLI vs. this app's own key** — same distinction as ResearchMesh's own
  CLAUDE.md: setting `ANTHROPIC_API_KEY` makes Claude Code bill per-token instead of
  using a Pro/Max subscription, even if one's active. `~/.bashrc`'s
  `alias claude='env -u ANTHROPIC_API_KEY claude'` hides the variable from just that
  one invocation (`env -u` scopes to the child process only) so `claude` falls back to
  subscription auth while this app keeps reading the same shell's key untouched.
  Don't suggest unsetting the variable itself; that breaks this app's own calls too.
- The ROUTER's OWN Claude model comes from `config.toml` (`[claude]
  claude_models`, a list) — the first entry is what every new session starts
  on. Ported from ResearchMesh (same file/mechanism, see that repo's own
  CLAUDE.md and `adding-model-command-to-swap-between-Anthropic-models.md` in
  its `/memories` for the full design history): that list is a live-refreshed
  cache, not hand-typed — `core/claude.py`'s `refresh_claude_models()` is
  TTL-gated (`model_scan_ttl_hours`, default 24), re-scanning Anthropic's real
  `/v1/models` via `fetch_live_models()` once the cache goes stale and
  rewriting `claude_models` in place, one entry per model family, newest-first
  except sonnet is always moved to the front. A failed scan (offline, bad key)
  touches nothing on disk. No env var override — `CLAUDE_MODEL` is NOT read
  here (an earlier, now-removed line in this file claimed it overrode
  `[claude] model`; that was the pre-`/model` single-string config, gone as
  of this port). Swapping mid-session is `/model swap`'s job (`core/
  cli.py`), and it affects **only the router's own reasoning model** — it has
  no bearing on which model a connected worker uses internally, that is
  entirely each worker's own `config.toml`. `e2e_test.py` has its own,
  separate `os.getenv("CLAUDE_MODEL", "claude-sonnet-5")` for picking a test
  model — unrelated to this app's actual config-loading path, not touched by
  this port.
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
- `CLAUDE_KERNEL_ENCRYPTION` — `auto` (default), `required`, or `off`, selecting
  the transport for the local `python` kernel: CurveZMQ-encrypted TCP, then IPC,
  then plaintext TCP, each tier printing why it fell through (`core/kernel.py`,
  copied verbatim from ResearchMesh — keep it that way). `required` turns an
  unencrypted kernel into a tool error instead of a silent fallback. It governs
  **this machine's kernel only**: a worker runs its own `python` tool in its own
  process, and reads this variable from *its* environment, not the router's.
- `[router] max_parallel` (default 8) — how many workers may be busy at once.
- `[router] timeout_seconds` (default 900) — per-call deadline; override per
  worker with `timeout_seconds` on its entry.
- **Worker bearer tokens** — each entry's `token_env` names the variable holding
  its token, sent as `Authorization: Bearer <token>`. No `token_env` means
  unauthenticated, by design. This is the consuming half of the contract
  `mcp_server.py --token-env` implements on the serving side; both ends normally
  read the *same* variable name, and a second name is only needed on a machine
  that both serves and consumes.
- **`[embeddings]` in config.toml** — settings for the `text_embeddings` tool
  (`core/text_embeddings.py`, copied verbatim from ResearchMesh): `url` (required
  — the tool errors by name until this is set), `model`, `request_format`
  (`"openai"` default or `"simple"`), `api_key_env` (same `token_env`-style
  indirection as the worker bearer tokens above — never the token itself), and
  `timeout`. Entirely commented out by default, and read fresh from disk on
  every call rather than cached at import, unlike `[router]`/`[claude].model`
  which `main.py` reads once at startup.
- **`[vision]` in config.toml** — settings for the `vision_query` tool
  (`core/vision.py`, copied verbatim from ResearchMesh): `url` (required — the
  tool errors by name until this is set), `model`, `max_tokens` (default 4000
  — tuned for a reasoning-capable vision model that can burn a low budget
  entirely on invisible `reasoning_content` before writing its real answer),
  `timeout` (default 180), and `api_key_env` (same `token_env`-style
  indirection as `[embeddings].api_key_env` above). Also entirely commented
  out by default and read fresh from disk on every call. **No automatic
  fallback to Claude's own vision lives in this tool** — if the server is
  unset or unreachable, it returns a `local_unavailable` status and stops;
  using Claude's own vision on the same image after that is a separate,
  explicit-consent decision made in conversation, never silent.
- **`[speak]` in config.toml** — settings for the `speak` tool (`core/speak.py`,
  copied verbatim from ResearchMesh): `enabled` (true/false, default true — a
  hard off-switch checked *before* `voice_model`, independent of whether a
  voice model is actually set up), `voice_model` (path to a Piper `.onnx` file;
  required, needs a matching `<path>.json` sidecar), `sink` (PipeWire sink name,
  falls back to the system default if unset), and `timeout` (subprocess
  timeout for synthesis AND playback each, default 30). Entirely commented out
  by default (this repo ships unconfigured, unlike ResearchMesh's own
  `config.toml`, which currently carries live hardware values as an explicitly
  flagged testing-state exception — see that repo's own inline comment). Read
  fresh from disk on every call. **⚠️ Requires the PyPI package `piper-tts`,
  NOT `sudo apt install piper`** — the apt package is an unrelated GTK app for
  configuring gaming mice, same name by coincidence.
- **`[listen]` in config.toml** — settings for the `listen` tool
  (`core/listen.py`, copied verbatim from ResearchMesh): `enabled` (same hard
  off-switch shape as `[speak].enabled`, checked before `device`), `device`
  (PipeWire source name to record from, `pactl list sources short` to find it;
  required), `model_size` (faster-whisper model size — `tiny`/`base`/`small`/
  `medium`/`large-v3`; default `"base"`), `default_duration_seconds` (default
  8), and `max_duration_seconds` (safety cap regardless of what was requested;
  default 30). Entirely commented out by default, same as `[speak]` above.
  Read fresh from disk on every call.
- **`[bash]` in config.toml** — `shell`: the interpreter both `bash`
  (`core/claude_learned_schemas.py`) and `interactive_run`
  (`core/processes.py`) actually execute commands through — both files, and
  this mechanism itself, copied verbatim from ResearchMesh (absolute path or
  bare name via `$PATH`; default `/bin/bash`, same fallback if
  unset/blank/unresolvable). Resolved once at import, not fresh per call.
  Ships un-commented with the safe default here, unlike `[speak]`/`[listen]`
  above — `/bin/bash` needs no hardware-specific value to be usable, so there
  is nothing to withhold. **If `shell` resolves to zsh**, `apply_shell_prelude()`
  (same module, also copied verbatim) prepends `setopt SH_WORD_SPLIT; unsetopt
  NOMATCH` to every command — confirmed against zsh's own FAQ as exactly the
  documented "classic differences" fix from bash, not assumed. Deliberately
  does NOT also set `KSH_ARRAYS` to fix zsh's 1-based array indexing, since
  that option changes what an unsubscripted `$array` means and makes braces
  mandatory for subscripts that don't need them in plain zsh — trading one
  divergence for a more invasive one; that gap is left as a `SYSTEM_PROMPT`
  fact instead. **`SYSTEM_PROMPT`'s local-tools section names which shell
  the LOCAL `bash` actually runs through** and that one remaining
  array-indexing gotcha — not a straight port of ResearchMesh's own
  paragraph, which also explains `/bin/sh` on the host (dropped here: only
  relevant to writing a standalone `#!/bin/sh` script, a poor match for this
  router's own bash usage, which the prompt itself frames as local
  bookkeeping rather than primary work). This is scoped strictly to the
  router's own local `bash` — it says nothing about any worker's shell,
  which is a separate, per-worker fact.
- **`core/bash_session.py`** — `bash_session`: persistent shell (`cd`/env/venvs/bg
  jobs survive across calls), copied verbatim from ResearchMesh, ported after the
  initial tool merge. Same idea as `core/kernel.py` but over `pexpect` instead of
  ZeroMQ, and reuses this repo's own `[bash].shell`/`apply_shell_prelude()` above
  so it can't drift from the stateless `bash` tool's shell choice or zsh handling
  — including two zsh-specific fixes ported alongside this file (a real spawn-time
  hang and a recovery-path corruption bug, both found live against a real zsh;
  see the module's own docstring, `_PS1_RESET` swaps in a `precmd()`-based
  mechanism there since zsh has no `PROMPT_COMMAND` at all).
  Module-level singleton shell; each command plus a `PROMPT_COMMAND='PS1=""'`
  reset plus a `printf` sentinel+`$?` are sent as ONE brace group, not separate
  lines — bash only consults `PROMPT_COMMAND` between top-level reads, never
  mid-compound-construct, so the reset always wins even against a command that
  reassigns `PROMPT_COMMAND` itself (conda/direnv), not just plain `venv`.
  Timeout sends Ctrl-C, gated by a real `tcgetpgrp` check rather than a
  sentinel-match alone (a still-alive raw-mode program like `less`/`vim`/`top`
  can echo the sentinel back itself) — if bash doesn't own the terminal after
  that, escalates to a full respawn (`restart:true`'s own path) instead of
  retrying on the same pty, reporting `state_reset: true`. In the
  `shutdown()` tuple. Not separately named in `SYSTEM_PROMPT` — that prompt's
  own local-tool examples (`bash`, `python`, `computer`, ...) are deliberately
  illustrative, not an exhaustive/numbered list the way ResearchMesh's own
  `SYSTEM_PROMPT` is, so there is no closed enumeration for this to be
  missing from.
- **`core/process_reaper.py`** — `reap_orphans()`: copied verbatim from
  ResearchMesh, ported alongside `bash_session.py`. Last-line exit safety net,
  independent of what any tool's own `shutdown()`/`cleanup()` claims to have
  handled — walks `/proc/<pid>/task/<TID>/children` for **every thread**, not
  just the main one (every blocking local tool here uses
  `asyncio.to_thread()`, so a forked child shows up under a worker thread's
  task entry, not the main thread's), SIGKILLs whatever's still alive, then
  reaps zombie direct children via a bounded `waitpid(-1, WNOHANG)` retry loop.
  Registered in `main.py` on the `AsyncExitStack`, pushed FIRST so it runs
  LAST — after every worker's own `cleanup()` and `local_tools.shutdown()`.
  Reports what it actually found and killed; empty is the expected common case.
- **TLS to a worker needs nothing here.** An `https://` url just works:
  `create_mcp_http_client` exposes no `verify` parameter to plumb, and none is
  needed, because httpx2 defaults to `truststore.SSLContext` — the OS trust
  store — with `SSL_CERT_FILE`/`SSL_CERT_DIR` as a per-process override. So a
  company CA or a paid certificate is entirely the worker's side of the job
  (`mcp_server.py --ssl-certfile/--ssl-keyfile`). Don't "add TLS support" here;
  there is nothing to add, and a hand-built `httpx2.AsyncClient` would drop the
  SDK's MCP timeout defaults for no gain.
- **`$VAR` in `[mcp].servers`** — `tomllib` does no substitution, so
  `_expand_paths()` expands `~` and `$VAR`/`${VAR}` in `command`, `url` and the
  *values* of `env`. Keys of `env` are variable names and are left alone, and
  **`description` is deliberately not expanded** — it is prose for the model, so
  a `$` in it is a dollar sign.
- Python 3.11+ — the floor is `tomllib`.

## What the smoke test guards

Not a port of ResearchMesh's. There is no local tool registry here, so the
checks are:

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

- No recursion or loop protection for a worker configured to point back here —
  open question, not yet resolved.
