# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

ResearchMesh-Router is a command-line agent that routes work across a fleet of
MCP worker agents on other machines, running independent work concurrently. It
also holds a full local toolset of its own, merged in from ResearchMesh.

> Where a comment or doc says the router owns no tools or executes nothing, the
> text is stale, not the code. Worker tools are namespaced, and a worker's
> schemas never reach this process's API request.

Two halves, in one `tools` array on every request:

- **Local tools** — plain names (`bash`, `python`, `computer`, `memory`,
  `browser_*`, `sql_query`, …), executed in-process on the router's machine.
- **Worker tools** — `<worker>__<tool>`, forwarded over MCP to another machine.

They cannot collide: a ResearchMesh worker advertises one tool (`delegate`),
every worker tool is namespaced with a `__` that no local name contains, and
`ToolManager.build` takes a `reserved` set of the local names, so uniqueness is
checked across the whole array. A duplicate 400s the entire request.

It is built for fleets of **interchangeable** workers: several machines running
the same agent software, differing in what is installed, attached or stored on
them. Nothing in it is specific to one worker implementation;
[ResearchMesh](https://github.com/nodormu/ResearchMesh) is the worker it was
built and tested against, not a dependency.

**This file is self-contained.** ResearchMesh's own CLAUDE.md is the place to
look when a worker's behaviour is the question (what `delegate` does with a
task, what its local tools are).

## Origin

A standalone project. The CLI shell, the Anthropic wrapper and the MCP client
began as copies from ResearchMesh (same author, MIT); knowing which parts are
inherited tells you where to be careful.

| File | Where it came from |
|---|---|
| `core/cli.py` | copied; adds `/workers` and `/dagent` (router-specific); `/voice`, `/listen`, `/model` and `/clear` are ports |
| `mcp_client.py` | copied, plus `timeout_seconds` |
| `core/__init__.py`, `config_edit.py`, `data.py`, `documents.py`, `files.py`, `output.py` | copied, byte-identical |
| `core/bash_session.py`, `claude.py`, `claude_learned_schemas.py`, `kernel.py`, `listen.py`, `memory.py`, `process_reaper.py`, `processes.py`, `speak.py`, `text_embeddings.py`, `vision.py` | same code as ResearchMesh; docstrings and comments here are shorter |
| `core/browser.py`, `browser_session.py` | `browser.py` copied; `value_secret` is in both. Launch modes, profiles, tabs, downloads and `submit` exist only here, with the launch code in `browser_session.py` |
| `core/computer.py` | copied; here it also has the Wayland backend (`core/wayland_input.py`) |
| `core/desktop_window.py`, `screen_find.py`, `dbus_loop.py` | new here: window control through KWin scripting, OCR-based find-on-screen, and the D-Bus loop that `wayland_input.py` and `desktop_window.py` share |
| `core/local_tools.py` | the registry; its `MODULES` list has no `midi1` entry (this repo has no MIDI tool) |
| `main.py`, `core/chat.py` | same skeleton; local-tool wiring restored, `SYSTEM_PROMPT` rewritten |
| `core/tools.py` | rebuilt for this project; only the result-formatting helpers survive |
| `smoke_test.py`, `e2e_test.py`, `e2e_worker.py`, `config.toml` | written here |

Keep tool-module code identical to ResearchMesh's wherever this repo has no
reason to diverge, so a fix in either repo is a copy of the code. Prose is not
kept in step (docstrings and comments are shorter here), so compare the parsed
code, ignoring docstrings, not the bytes. The files that differ in code are
`browser.py`, `chat.py`, `cli.py`, `computer.py`, `local_tools.py` and
`tools.py`; five files exist only here, and `midi1.py` exists only in
ResearchMesh. Anything else that differs in code is drift worth explaining, so
re-run the comparison instead of trusting this list. `mcp_client.py` differs by
the `timeout_seconds` parameter and its two uses.

**MCP SDK majors.** `mcp_client.py` and `core/tools.py` are the two files that
broke on mcp 1.x to 2.x: the transport function was renamed, headers moved onto
an httpx2 client built by `create_mcp_http_client`, `read_timeout_seconds`
changed from `timedelta` to `float`, and the model fields became snake_case. An
mcp 3.0 would land there, and in ResearchMesh too. `mypy .` reports a break,
since it checks against the installed packages.

## Commands

Run the app **from the repo root**:

```bash
python main.py
```

Needs `ANTHROPIC_API_KEY` in the shell (no `.env` is loaded), plus one variable
per authenticated worker, named by that worker's `token_env` in `config.toml`.

```bash
pip install -r requirements.txt
playwright install chromium   # pip installs the package, not the browser itself
```

The full install walkthrough (apt packages, the `computer` tool's X11/Wayland
setup, LibreOffice/Pandoc) is in `README.md`'s "Setup (Linux)". This repo needs
the same backings as ResearchMesh's local tools.

Check every configured worker standalone (connect, list tools, report failures):

```bash
python mcp_client.py
```

Three gates, all expected clean:

```bash
ruff check .
mypy .
python smoke_test.py
```

`smoke_test.py` needs no API key, network or workers: it builds a fleet of
fakes and checks the invariants listed under "What the smoke test guards".

Outside the gates (they spend real tokens or need a desktop):

```bash
python e2e_test.py     # ~15s, needs ANTHROPIC_API_KEY
```

It launches two `e2e_worker.py` subprocesses over stdio MCP and covers what
fakes cannot: the duplicate-name 400 is the API's real behaviour, namespacing
survives a real transport, and a real model issues both calls in one turn from
the `[worker: …]` headers alone. Run it after touching `core/tools.py`,
`mcp_client.py` or `SYSTEM_PROMPT`.

`python test_model_compat_live.py` (real API, about 9 requests, exits 2 without
a key) sends the full local tool list to every model in `config.toml` through
the real `Claude.chat()`. Run it after touching `core/claude.py` or the tool
list, or when adding a model.

The behavioural scripts `test_processes.py`, `test_bash_session.py`,
`test_process_reaper.py`, `test_listen.py`, `test_browser_secret.py`,
`test_wayland_input.py`, `test_desktop_window.py` and `test_screen_find.py` need
no API. `test_browser_mode.py` needs a display:
`xvfb-run -a python test_browser_mode.py < /dev/null`.

## Why this repo exists

A ResearchMesh instance served as `mcp_server.py` exposes one tool, `delegate`.
Three workers advertise three tools with the same name, and the API rejects the
whole request:

```
400 invalid_request_error: tools: Tool names must be unique.
```

ResearchMesh's `ToolManager` makes it worse: `get_all_tools` copies `t.name`
verbatim and `_tool_owners` uses `setdefault`, so even without the 400 only the
first worker would be reachable, silently.

Claude Code is unaffected: it namespaces MCP tools as `mcp__<server>__<tool>`.
This repo is for the case where the boss is itself a ResearchMesh-shaped CLI.

## Architecture

Request flow: **CLI input → Chat.run() agentic loop → Claude API + local tools +
worker MCP tools**.

- **`main.py`** — entrypoint. Reads config, connects each `[mcp].servers` entry
  (a worker that fails is reported and skipped, never fatal), builds the
  worker-description map, and wires a `Chat` into the `CliApp` loop. Registers
  `local_tools.shutdown` on the `AsyncExitStack` next to each worker's
  `cleanup`.

- **`core/tools.py`** — differs substantially from ResearchMesh's and is the
  reason this repo exists. Three changes:
  1. *Namespacing.* Tools are declared as `<worker>__<tool>` and the prefix is
     stripped before the call goes out. `_legalise()` enforces the API's
     `^[a-zA-Z0-9_-]{1,128}$`: it substitutes illegal characters, truncates with
     a deterministic hash suffix (a name that changed between requests would
     cost the prompt cache every turn) and disambiguates collisions.
  2. *Worker identity.* Each tool's description gets a `[worker: name] <blurb>`
     header from the config entry's `description`. Without it, namespaced tools
     would carry byte-identical text, because ResearchMesh hardcodes one
     `_DELEGATE_DESCRIPTION`. It is config-side, so no worker redeploy is needed
     and non-ResearchMesh servers work.
  3. *Fan-out.* `execute_blocks` groups blocks by owning worker, runs the groups
     concurrently and each group in order: a ResearchMesh worker serialises
     `delegate` behind an `asyncio.Lock` (one mouse, one browser page, one
     kernel).

  It takes a `Worker` Protocol (`list_tools`, `call_tool`), not `MCPClient`, so
  `smoke_test.py` can use fakes; fleets are `Mapping[str, Worker]` because
  `dict` is invariant in its value type. `ToolIndex` is built once per user turn
  by `Chat` and passed to `execute_blocks`; ResearchMesh's version makes a
  `list_tools` round trip per client per tool-use iteration.

- **`core/chat.py`** — the agentic loop, structurally ResearchMesh's.
  `_run_tool_uses` tries each block against `local_tools.execute`, then
  `ToolManager.execute_blocks`. `_local_result_to_content` turns the
  `core.output.image_result` marker into an `image` block (worker images take
  `_call_one`). Two differences: results are reassembled in the original block
  order via a `by_id` dict, and local tools are declared first in `tool_defs`,
  so the front of the cached prefix stays stable when a worker drops out.

  `SYSTEM_PROMPT` is a full rewrite. It leads with the local/remote split (a
  plain name is this machine, `<worker>__` is another), because nothing in a tool
  definition says which. It states that workers share no filesystem or state
  with each other or with this machine, that `delegate` wants an outcome rather
  than a command, how `session` ids work and that a fresh one does not clear
  `/memories`, that batching across workers parallelises but batching within one
  does not, and that delegations are slow and must not be polled. It pushes
  against the local tools, which match any concrete command more directly: the
  failure mode is doing a worker's job on the wrong machine. It also says when
  to use `web_fetch`, `browser_navigate` and `computer`, and that a one-time code
  is not a vault secret. Keep it factual; update it if the routing model
  changes.

- **`core/claude.py`** — thin Anthropic SDK wrapper, same as ResearchMesh's. It
  posts to `client.beta.messages.create` with an empty `BETAS`:
  `computer_toolset_20260801` needs no beta header, and the beta endpoint is a
  superset of the plain one. It returns `BetaMessage`, which is not a subclass of
  `Message`, so the isinstance checks use the `_RESPONSE_TYPES` tuple; without it
  the response object lands in `content` instead of its blocks. Top-level
  `cache_control` works on both endpoints. A worker's beta-gated tools are its
  own concern: it makes its own API call, and its schemas never reach this
  request.

  Constraints on `chat()`:
  - **No sampling parameters.** Current models (Sonnet 5, Opus 5, Opus 4.7+)
    reject a non-default `temperature`, `top_p` or `top_k`. Steer with
    `SYSTEM_PROMPT`.
  - **No `budget_tokens`.** Adaptive thinking replaced it; the old
    `{"type": "enabled", "budget_tokens": N}` is a 400. The depth knob is
    `output_config={"effort": …}`.
  - **`max_tokens=20000`**, shared by thinking and the reply. A lower cap let a
    single large `create` call be cut off mid-tool_use, leaving an unanswered
    `tool_use` that poisons every later turn. It stays under the SDK's
    ~21,333-token non-streaming ceiling (`self.client = Anthropic()` sets no
    `timeout=`, so `client.messages.create` raises "Streaming is required…"
    above it); streaming would need rework of how
    `core/chat.py` reads `response.content`, `stop_reason` and `usage` as one
    object.
  - **Prompt caching fails silently.** A prefix under the model's minimum (1024
    tokens on Sonnet 5) is not cached, with no error, and any early byte change
    invalidates everything after it. `CLAUDE_SHOW_USAGE=1` is the only way to
    confirm it is landing.

  **Per-model tool compatibility is self-healing.** Not every tool type works on
  every model (Haiku 4.5 rejects `computer_toolset_20260801`), and the API fails
  the whole request over one, so `/model swap` to such a model would 400 every
  turn. `Claude.chat()` catches a `BadRequestError` matching Anthropic's fixed
  "does not support tool types: ..." wording, parses the types, records them in
  `self._unsupported_by_model` (keyed by model), filters them out and retries
  once with a `[model compat]` console note. Later requests for that model
  filter up front, so only the first turn pays a retry and nothing is
  hand-maintained. `web_search` and `web_fetch` declare
  `allowed_callers: ["direct"]` in `claude_learned_schemas.py`: unset, Haiku
  rejects them ("does not support programmatic tool calling") because it cannot
  be a `code_execution` caller, which this project never uses.

- **Haiku 4.5 has no computer tool (known limitation).** The compat handler
  drops the toolset after the first rejected request per model per process, and
  every other tool keeps working. `computer_20250124` (beta header
  `computer-use-2025-01-24`) is accepted on Haiku but not declared: it needs a
  per-request beta header and a second executor path (`name: "computer"` with
  `input.action`; `computer.handles("computer")` is False on purpose, asserted in
  the smoke test), and declaring both computer tools in one request is a 400 on
  every model. After the computer tool was used on another model in the same
  conversation, `/model swap` to Haiku fails every turn with a 400
  (`toolset_name 'computer' ... is not the family of a declared toolset entry`);
  swap back or `/clear`. `/dagent` sends no local tools, so the same applies
  there (checked at the API level only).

- **`mcp_client.py`** — kept close to the copy it came from, so the two stay
  easy to compare when the MCP SDK next changes shape; resist tidying it. One
  divergence, `timeout_seconds`: both defaults are too short for a worker that
  runs a whole agentic loop before replying (`create_mcp_http_client` reads for
  300s), and when one fires the work is already done on the far side and lost.
  It applies in two independent places: the httpx2 read timeout, and
  `ClientSession(read_timeout_seconds=…)` (a plain float in mcp 2.x, a
  `timedelta` in 1.x). Connect stays at 15s so a switched-off machine fails
  fast.

- **`core/cli.py`** — carries `/workers` and `/dagent` (router-specific) and
  ports of `/voice`, `/listen`, `/model` and `/clear`. `/voice` and `/listen`
  reuse `core/speak.py` and `core/listen.py` through a shared `_submit()`, which
  threads `thinking`, `remote_only` and `worker` so a dictated turn respects any
  `/dagent` state (in practice a dictated turn is always a plain turn).
  `/think <text>` strips the prefix and sends the text with adaptive thinking on
  (`thinking=True`); it is checked before `/dagent`, so `/think /dagent <task>`
  combines the two.

  - **`/clear`** (also `/reset`) — empties `self.messages` and keeps the fleet
    connected. It is the way out of the two failures that persist for the life of
    the process: an unanswered `tool_use` block, and a conversation past the
    context window. Without it the only way out is killing the router, which
    drops every worker connection and worker-side `session` id.
    `Chat._report_api_failure` says which of the two you hit, checking for
    orphaned `tool_use` ids directly. Its size report is in characters, not
    tokens: `count_tokens` rejects the server tools (`web_search`, `web_fetch`).
  - **`/model`** (bare) lists `config.toml`'s `[claude] claude_models` with
    1-based indices and marks the current one. The list comes from
    `load_claude_models()` in `core/claude.py`, re-read on every call, so an
    edit shows without a restart. **`/model swap <name or index>`** resolves the
    argument with `resolve_model_swap()` and sets
    `self.agent.claude_service.model` directly (`Claude.chat()` reads it fresh
    each call). It is session-only: it never writes `config.toml`, so a new
    session starts on `claude_models[0]`. It affects only the router's own
    reasoning model. An unrecognized name or index, or a bare `/model swap`, is
    rejected with a message.
  - **`/model <worker>`** and **`/model <worker> swap <name or index>`** — call a
    connected worker's own `model` MCP tool directly
    (`self.agent.clients[worker_id].call_tool("model", ...)`), bypassing Claude
    and `ToolManager`, like `/workers`. The worker check (`sub in
    self.agent.clients`) runs before the bare-`/model` parsing, so a worker named
    `swap` is still a worker. It is not built on `Chat.split_worker()`, which
    requires a non-empty remainder (it serves `/dagent`). The precedence check and
    argument parsing are in `Chat.resolve_worker_model_request()` (pure) and the
    call and formatting in `Chat.call_worker_model()` (async), so
    `check_model_worker_dispatch()` in `smoke_test.py` tests them without a REPL.
    Every printed line is prefixed `[worker: <name>] ` (the tag `core/tools.py`
    uses), so a worker's result never reads like the router's own `/model`
    output. The worker's `model` tool owns its live-scan and TTL decision. The
    router's own Claude can also call that tool in an ordinary turn, since it is
    namespaced like `delegate`; the command exists for convenience.
  - **`/workers`** — the workers that are up, with the exact names `/dagent`
    takes, answered locally without spending a turn. It rebuilds the index each
    time, since a cached listing would report a machine that went down. A worker
    that failed to connect or has died is absent: absent and unreachable are the
    same thing to the model.
  - **`/dagent [worker] <task>`** — one turn with the local tools withheld,
    optionally pinned to one machine. It filters `tools` instead of instructing
    the model: a local `bash` matches any concrete command better than a
    `delegate` that takes minutes, so an instruction is a preference and an
    absent schema is a fact. `_DELEGATE_ONLY_SUFFIX` is appended to the system
    prompt because `SYSTEM_PROMPT` still names local tools. Two accepted costs:
    it misses the prompt cache in each direction (tools render ahead of `system`,
    so changing the list invalidates what follows, on this turn and the next
    ordinary one), and it aborts instead of sending an empty tool list, which
    would leave a turn with no tools; it pops the user message back off
    `self.messages`.
  - **`/voice [on|off]`** — toggles `self.auto_speak` (default off), which only
    gates whether replies are also spoken through `speak.py`'s `_run`. It does
    not affect whether `speak` and `listen` are reachable as tools
    (`[speak].enabled`, `[listen].enabled`) or whether a `/listen` dictation is
    submitted.
  - **`/listen [N]`** — records `N` seconds from the configured mic (default
    `[listen].default_duration_seconds`), transcribes locally through
    `listen.py`'s `_run` (faster-whisper), and auto-submits the transcript as a
    turn through `_submit()`, whether `/voice` is on or off. There is no edit
    step: a garbled transcript is sent as is. `/listen abc` reports an error and
    submits nothing; `[listen].enabled = false` or an unset `device` reports
    `disabled` or `not_configured` and never opens the microphone.

- **`core/bash_session.py`** — `bash_session`: one persistent shell, so `cd`,
  exports, venvs and background jobs survive across calls. Same idea as
  `core/kernel.py`, driven over a pty with `pexpect`; a module-level singleton.
  It takes its shell from `[bash].shell` and `apply_shell_prelude()`, so it
  cannot drift from the stateless `bash` tool.
  - Framing: each command, a prompt reset and a `printf` of a per-spawn
    sentinel plus `$?` are sent as one brace group. bash reads `PROMPT_COMMAND`
    only between top-level reads, never inside a compound command, so the reset
    wins even against a command that reassigns `PROMPT_COMMAND` (conda,
    direnv).
  - Shells: bash, zsh and dash. `_PS1_RESET` is `PROMPT_COMMAND='PS1=""'` on
    bash, a `precmd()` function on zsh, and a plain `PS1=''` on dash, which has
    no prompt hook. zsh also needs `_ZSH_SESSION_PRELUDE`, which disables its
    line editor. fish, tcsh and ksh hang or need different grouping syntax and
    are unsupported.
  - Timeout: sends Ctrl-C, then checks with `os.tcgetpgrp` that bash owns the
    terminal again. A sentinel match alone is not enough, because a raw-mode
    program such as `less`, `vim` or `top` can echo the sentinel itself. If
    bash does not own the terminal, it respawns a fresh shell through the path
    `restart: true` uses and returns `state_reset: true`.
  - `local_tools.shutdown()` calls `bash_session.shutdown`.

- **`core/processes.py`** — `interactive_run`: spawns a command on a pty and
  answers its prompts (passwords, `[y/N]`, ssh host keys) from a `steps` script
  the model supplies. Each step carries exactly one reply source:
  - `send` — a literal reply the model writes.
  - `send_env` — the NAME of an environment variable, read locally.
  - `send_secret` — the NAME of a `pass` entry, resolved locally with
    `pass show`. Only the first line is used. A `pass show` that outlasts
    `_SEND_SECRET_TIMEOUT` returns an error asking the user to unlock the GPG
    key in their own terminal first.

  A real secret sent as a literal `send` passes through Anthropic's API twice,
  once to the model and once back in its tool call, so use `send_env` or
  `send_secret` for those. `pass` is GPG-backed and needs no desktop session;
  `secret-tool`/libsecret needs a keyring daemon and does not work headless.

  **Redaction.** `send_env` and `send_secret` replies are always treated as
  secret, whatever the step's `secret` field says. `_redact()` makes a single
  pass over the complete final transcript, so a child process that echoes the
  value back later is also scrubbed. It covers the forms in `_secret_forms()`:
  percent, form, HTML, JSON, hex, and base64 (both alphabets, padded or not,
  at every alignment, so `Authorization: Basic ...` is covered). Derived forms
  shorter than 6 characters are dropped because they would match ordinary text.
  A reversed or otherwise transformed copy is not caught.
  `test_processes.py` `check_secret_redacted_even_when_echoed_back_later`
  covers the echo case.

  **Name gate.** The model does not choose which entry gets decrypted.
  `resolve_secret()` decrypts only an entry whose name the user typed in one of
  their own messages this session. `Chat.run()` passes each user message to
  `note_user_message()`, which records every vault entry named in it (whole-name
  match, not a substring) in `_confirmed_secret_entries`. Any other name, or
  `"?"`, returns the fixed prompt from `_select_entry_prompt()`
  (`"please select the cred name I need to use:"` plus every real entry) and
  decrypts nothing. A confirmed name stays confirmed for the rest of the
  session and for any use; it is not tied to the request it was named for.
  The match is on the whole name anywhere in the message, so a passing mention
  ("push it to github" with an entry named `github`) confirms it. A confirmed
  entry is typed into whatever page is open: a page that talks the model into
  filling its form receives the real value, and scrubbing does not cover that,
  because the value never returns through the model.

  **Entry names.** `_select_entry_prompt()` reads `$PASSWORD_STORE_DIR`
  (default `~/.password-store`) and walks it for `*.gpg` filenames; nothing is
  decrypted. It does not parse `pass ls`, whose tree drawing loses the folder of
  a nested entry (`aws/prod` becomes `prod`, not a valid `pass show` argument).
  A failed `pass show` gets `_available_entries_hint()` appended: the entry
  names from `pass ls`, never values. `_SEND_SECRET_TIMEOUT` (30 s) bounds
  `pass show` because an uncached GPG key can raise a `pinentry` popup on the
  user's screen, and answering it takes longer than a few seconds; a timeout
  still fails clearly instead of waiting out the whole `interactive_run`
  timeout.

  **Browser.** `browser_fill` takes exactly one of `value` or `value_secret`.
  A `value_secret` goes through the same `resolve_secret()` gate, is typed into
  the field, and is added to `_filled_secrets` in `core/browser.py`. Every
  browser tool result is scrubbed of those values before clipping, because a
  clip can cut a secret in half and leave a prefix. Delegated task text is sent
  like any message, so a login that needs a vault secret runs in the router's
  own browser. `test_browser_secret.py` runs against a local login form that
  reflects the password back.

- **`core/browser.py`, `core/browser_session.py`** — the `browser_*` tools
  (Playwright, DOM-based). `browser_session.py` owns the launch.
  `browser_navigate` takes `mode` and `profile`; `headed` (true or false) is an
  alias for `headed` or `headless`, and giving both is an error. A bad value is
  rejected before the browser is touched, so it never drops the open page.
  - Modes: `headless` (default) uses installed Chrome if present, else bundled
    Chromium, with Chrome's user agent corrected. `headed` opens a visible
    window and needs a display. `virtual` runs Chrome on a private Xvfb and
    needs Chrome and Xvfb. `real` starts installed Chrome normally on a
    127.0.0.1 debug port and attaches over CDP; it needs Chrome and a display,
    and always gets its own profile directory because Chrome refuses a debug
    port on its default profile.
  - Playwright modes pass `--disable-blink-features=AutomationControlled`:
    without it `navigator.webdriver` is true and Cloudflare Turnstile fails.
  - `profile` is 1-40 characters of letters, digits, `-` or `_`: a mode-700
    directory under `$XDG_CACHE_HOME/researchmesh/browser-profiles` (default
    `~/.cache`). Without one the profile is temporary. A change of mode or
    profile restarts the browser and drops the page and any login not kept in a
    profile.
  - A click that opens a tab switches to it; `browser_tab` lists, switches and
    closes.
  - Downloads go to `~/Downloads` (`RESEARCHMESH_DOWNLOAD_DIR` overrides) under
    a unique name and are listed as `Downloaded:` lines. A CDP-attached Chrome
    overwrites a same-name file, so it writes to a staging directory first.
  - Reports carry a `Human check:` line. A fresh default-mode visit that a check
    stops is reopened once in `virtual` mode, never `real`, which opens a
    window. If `virtual` is not possible, the headless report stands.
  - `browser_fill` takes `submit`: press Enter and return the next page in the
    same call, so a one-time code goes in within its few seconds. The router
    prompt in `core/chat.py` says such a code is not a vault secret and may come
    through the chat, and says which browsing tool to use.
  - `test_browser_mode.py` covers all of it. Run it as
    `xvfb-run -a python test_browser_mode.py < /dev/null` to keep windows off
    the desktop.

- **`core/computer.py`, `core/wayland_input.py`, `core/dbus_loop.py`** —
  `computer` is the client toolset `computer_toolset_20260801`; it
  expands into 17 member tools (`computer._MEMBERS`), and `execute()` refuses
  any other name. It runs on a pyautogui-shaped backend.
  `computer._wayland_session()` picks it, and is what tests patch. On a Wayland session the backend is
  `wayland_input.py`: one xdg-desktop-portal RemoteDesktop + ScreenCast session
  held in-process on a private asyncio loop (`dbus_loop.Loop`, `dbus-next`).
  - The desktop may ask for approval (`_APPROVAL_SECONDS`, 150). KDE shows a
    "Remote Control" tray icon with an "End" entry; after End, the next action
    starts a new session.
  - A scroll unit is 10 portal steps (`_STEPS_PER_NOTCH`), about 120 px in
    Chrome.
  - The screen is one monitor: the leftmost approved stream, or the index in
    `CLAUDE_COMPUTER_MONITOR`. Screenshots come from `spectacle` or `grim`,
    cropped to it.
  - `position()` returns the last pointer position the backend set, because the
    portal cannot read it back.
  - `test_wayland_input.py` drives a fake portal. The real session needs the
    approval dialog and is exercised by hand.
- **`core/desktop_window.py`** — `desktop_window`: list, focus, move and resize
  windows. KDE Plasma only, on Wayland or X11. A one-shot KWin script runs
  inside the compositor and calls back (`callDBus`) into a service this process
  exports on the session bus. The script embeds its arguments as JSON literals,
  so nothing the model sends is interpreted as code.
  `test_desktop_window.py` covers it and also lists the real windows when a KDE
  session is reachable.
- **`core/screen_find.py`** — `screen_find`: locates on-screen text and buttons
  with `tesseract`, in the `computer` tool's declared coordinates. It reads the
  screen through `computer.capture()` (X11 or the Wayland backend) in two
  passes: word-level OCR in normal and inverted form, and a detector for
  solid-colour rectangles of button size whose crops are read upscaled, because
  plain OCR misses light text on coloured buttons. Only the match list is
  returned. `test_screen_find.py` covers it.

- **`core/process_reaper.py`** — `reap_orphans()`: the last-line exit safety
  net, independent of any tool's own `shutdown()`. It walks
  `/proc/<pid>/task/<TID>/children` for every thread, not only the main one:
  the blocking local tools run under `asyncio.to_thread()`, so a forked child
  sits under a worker thread's entry. It SIGKILLs whatever is still alive, then
  reaps zombie direct children with a bounded `waitpid(-1, WNOHANG)` retry
  loop, because a zombie does not appear the instant after the kill. Linux
  only; it reads `/proc` defensively, so a kernel without it finds nothing
  instead of crashing exit. `main.py` registers it on the `AsyncExitStack`
  first so it runs last, after every worker's `cleanup()` and
  `local_tools.shutdown()`. It prints what it killed, or
  `[shutdown] clean exit, no leftover processes`.

## Runtime configuration

- `ANTHROPIC_API_KEY` — from the shell. `main.py` keeps an explicit `os.getenv`
  reference to it; do not remove it.
- **Claude Code CLI vs. this app's key** — setting `ANTHROPIC_API_KEY` makes
  Claude Code bill per token instead of using a Pro/Max subscription. The
  `~/.bashrc` alias `claude='env -u ANTHROPIC_API_KEY claude'` hides the
  variable from that one invocation only. Do not suggest unsetting the
  variable itself; this app reads it.
- **Router model** — `config.toml` `[claude] claude_models` (a list); the first
  entry is the model every new session starts on. The list is a cache, not
  hand-typed: `core/claude.py` `refresh_claude_models()` is gated by
  `model_scan_ttl_hours` (default 24), scans `/v1/models` through
  `fetch_live_models()`, and rewrites `claude_models` in place, one entry per
  model family, newest first, sonnet moved to the front. A failed scan
  (offline, bad key) writes nothing. No env var overrides it: `CLAUDE_MODEL`
  is read only by `e2e_test.py`. `/model swap` changes only the router's own
  model; each worker uses the model in its own `config.toml`.
- `CLAUDE_SHOW_USAGE=1` — per-request token and cache counters. The tool list
  is built from live workers, so a worker dropping out mid-session changes the
  cached prefix and loses the cache hit. Declaring the local tools first limits
  this but does not prevent it.
- `CLAUDE_MEMORY_DIR` — where the local `memory` tool's `/memories` tree lives.
  **Set it explicitly.** It defaults to `./memories` relative to the working
  directory, so otherwise the router creates its own store in this repo. A
  ResearchMesh worker over stdio is safe because ResearchMesh's `mcp_server.py`
  chdirs to its own root first; any other stdio server with relative-path state
  inherits the router's working directory, since `main.py` passes no `cwd`.
- `CLAUDE_KERNEL_ENCRYPTION` — `auto` (default), `required` or `off`: the
  transport for the local `python` kernel. `auto` tries CurveZMQ-encrypted TCP,
  then IPC, then plaintext TCP, printing why each tier fell through
  (`core/kernel.py`, same code as ResearchMesh's copy; keep it that way).
  `required` turns an unencrypted kernel into a tool error. It governs this
  machine's kernel only; a worker reads the variable from its own environment.
- `CLAUDE_DISPLAY_SIZE` — `WxH`, the logical display size the `computer` tool
  declares to the model (default `1280x800`). Captures are downscaled to it and
  coordinates are scaled back to native pixels. An unparsable value falls back
  to the default.
- `CLAUDE_COMPUTER_FORCE=1` — use X11/XTEST on a Wayland session anyway
  (XWayland-only setups, nested X servers such as Xvfb). Without it a Wayland
  session goes through the xdg-desktop-portal in `core/wayland_input.py`, and
  `computer` refuses when that route is unavailable.
- `CLAUDE_COMPUTER_MONITOR` — monitor index, counted left to right, for the
  Wayland route. A missing or out-of-range value selects the first monitor.
- `[router] max_parallel` (default 8) — how many workers may be busy at once.
- `[router] timeout_seconds` (default 900) — per-call deadline; override per
  worker with `timeout_seconds` on its entry.
- **Worker bearer tokens** — each entry's `token_env` names the variable that
  holds its token, sent as `Authorization: Bearer <token>`. No `token_env`
  means unauthenticated. A named variable that is unset prints a warning and
  connects without auth. The serving half is ResearchMesh's
  `mcp_server.py --token-env`; both ends normally read the same variable name,
  and a second name is needed only on a machine that both serves and consumes.

- **`[embeddings]` in config.toml** — settings for `text_embeddings`
  (`core/text_embeddings.py`): `url` (required; the tool errors by name until
  it is set), `model`, `request_format` (`"openai"` default, or `"simple"`),
  `api_key_env` (names a variable, never the token itself) and `timeout`
  (default 30). Every key ships commented out. The table is read from disk on
  every call, unlike `[router]`, which `main.py` reads once at startup.
- **`[vision]` in config.toml** — settings for `vision_query`
  (`core/vision.py`): `url` (required; errors by name until set), `model`,
  `max_tokens` (default 4000; a reasoning vision model can spend a small budget
  entirely on `reasoning_content` before it writes an answer), `timeout`
  (default 180) and `api_key_env` (same indirection as `[embeddings]`). Every
  key ships commented out; read on every call. An unset or unreachable server
  returns `{"status": "local_unavailable"}` and stops. The tool never falls
  back to Claude's own vision; that is a separate decision the user confirms in
  conversation.
- **`[speak]` in config.toml** — settings for `speak` (`core/speak.py`):
  `enabled` (default true; a hard off-switch checked before `voice_model`),
  `voice_model` (path to a Piper `.onnx` file with a matching `<path>.json`
  sidecar; required), `sink` (PipeWire sink name; the system default if unset)
  and `timeout` (default 30, applied to synthesis and playback separately).
  Every key ships commented out; read on every call. Needs the PyPI package
  `piper-tts`, not `sudo apt install piper`, which installs an unrelated GTK
  app for configuring gaming mice.
- **`[listen]` in config.toml** — settings for `listen` (`core/listen.py`):
  `enabled` (default true; checked before `device`), `device` (PipeWire source
  name, found with `pactl list sources short`; required), `model_size`
  (faster-whisper size: `tiny`, `base`, `small`, `medium` or `large-v3`;
  default `base`), `default_duration_seconds` (default 8) and
  `max_duration_seconds` (cap on any requested duration; default 30). Every key
  ships commented out; read on every call.
- **`[bash]` in config.toml** — `shell`: the interpreter for the local `bash`
  tool (`core/claude_learned_schemas.py`), `bash_session` and `interactive_run`
  (`core/processes.py`). An absolute path or a bare name resolved through
  `$PATH`; unset, blank or unresolvable falls back to `/bin/bash`. Resolved
  once at import, so a change needs a restart. Ships set to `/bin/bash`.
  - If `shell` resolves to zsh, `apply_shell_prelude()` prepends
    `setopt SH_WORD_SPLIT; unsetopt NOMATCH` to every command. `KSH_ARRAYS` is
    not set: it changes what an unsubscripted `$array` means and requires
    braces on subscripts. zsh's 1-based array indexing is the remaining
    difference, stated in `SYSTEM_PROMPT` (`core/chat.py`), whose local-tools
    section also names the shell the local `bash` runs through.
  - This is the router's local shell only. A worker's shell is set by that
    worker's own `config.toml`.

- `RESEARCHMESH_DOWNLOAD_DIR` — where browser downloads land (default
  `~/Downloads`); see the `core/browser_session.py` entry under Architecture.
- `PASSWORD_STORE_DIR` — the `pass` store read for vault entry names (default
  `~/.password-store`); see the `core/processes.py` entry under Architecture.
- **TLS to a worker needs nothing here.** An `https://` url works as is:
  `create_mcp_http_client` takes no `verify` parameter, and none is needed,
  because httpx2 defaults to `truststore.SSLContext` (the OS trust store), with
  `SSL_CERT_FILE`/`SSL_CERT_DIR` as a per-process override. A company CA or a
  paid certificate is the worker's side (`mcp_server.py --ssl-certfile` and
  `--ssl-keyfile` in ResearchMesh). Do not add TLS support here, and do not
  build a bare `httpx2.AsyncClient`: it drops the SDK's MCP timeout defaults.
- **`$VAR` in `[mcp].servers`** — `tomllib` does no substitution, so
  `_expand_paths()` expands `~` and `$VAR`/`${VAR}` in `command`, `url` and the
  values of `env`. Keys of `env` and `token_env` are names and are left alone.
  `description` is not expanded: it is prose for the model, so a `$` in it is a
  dollar sign.
- Python 3.11+ (`requires-python`): the floor is `tomllib`.

## What the smoke test guards

`smoke_test.py` needs no API key, no network and no running workers. It checks:

1. every module imports and byte-compiles;
2. worker tool names are namespaced, unique and legal for the API. This is the
   regression this repo exists to prevent: two workers exposing the same tool
   name is a 400 (`Tool names must be unique`), and nothing else catches it
   until a second worker is connected;
3. a call on `worker__tool` reaches that worker as the bare `tool`;
4. a worker whose `list_tools` fails is skipped, not fatal;
5. execution fans out across workers, stays serial within one, and
   `max_parallel` bounds it. A sequential `execute_blocks` would pass every
   other check, only slower;
6. every `tool_use` block gets exactly one `tool_result`, in the original
   order, including for an unknown tool. An unanswered block makes every later
   request in the session a 400 about unresolved ids;
7. local tool names are legal and cannot collide with a namespaced worker name
   (the `reserved` set);
8. a turn mixing local and worker calls returns one result per block in the
   original order. `_run_tool_uses` assembles results from two executors, and
   appending worker results after local ones reorders every mixed turn;
9. `/dagent` withholds every local tool schema;
10. `/clear` and the diagnostics tell an orphaned `tool_use` from a full
    context;
11. `/model`: the command, the TTL-gated refresh, worker dispatch and the
    model-compat handler;
12. per-model tool handling, the computer toolset, `cursor_position` and the
    web tools' `allowed_callers`, on a fake API.

`check_docs_match_code` ties the docs to the code: README.md's stated tool count
(`N local tools`, `N local +` or `tools, not N`) must equal
`len(local_tools.TOOLS)`, and CLAUDE.md must name every module file that
`core/local_tools.py` imports. The local checks run `bash` with no `command`,
which returns an error string without running anything, so the file makes no
network calls and has no side effects.

## Conventions carried over from ResearchMesh

- **Blanket `except Exception` is deliberate.** `BLE001` is ignored in
  `pyproject.toml`. A worker on another machine can fail in any way, and every
  `tool_use` owes a `tool_result` in the next message; an exception that
  escapes poisons every later request in the session. Do not narrow these.
- **Cleanup paths must not fail, and must not fail silently**: a blanket catch
  plus a `print()`.
- **Run the app from the repo root.** `memory`'s `CLAUDE_MEMORY_DIR` defaults to
  the relative `./memories`, so the working directory decides which memory
  store you get. `config.toml` is found relative to `main.py`, not the working
  directory.
- **No approval gating.** The router executes local tools and sends whatever it
  decides to any worker, and each worker executes without approval too: two
  machines of unapproved execution from one prompt.

## Adding a worker

A config edit, not a code change: add an entry to `[mcp].servers` with a `name`,
a `url` or `command`, and a `description` that would change a routing decision.
Nothing else in the codebase enumerates workers, so there is no per-tool
checklist as in ResearchMesh.

## Deliberately not built

- Recursion or loop protection for a worker configured to point back at this
  router. Ask the user before designing one.
