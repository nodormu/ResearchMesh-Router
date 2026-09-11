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

**ResearchMesh-Router is NOT an MCP server, permanently — that's out of scope, not a
gap.** It connects *out* to ResearchMesh workers, or other MCP servers/agents/etc;
nothing connects *in*. That is what keeps the tool names unambiguous. One practical
consequence: Claude Code can reach each worker directly, but can't drive the whole
mesh through one endpoint, and this router can't itself be a worker in someone else's
fleet.

**It is a less restrictive orchestrator than Claude Code.** Fewer guardrails: no
approval prompts, no permission model, no context compaction. It runs any
program, command or script your user can run, on this machine and on every
worker, without babysitting. That is the point — and the risk.

## What it can do

**22 local tools**, plus one per connected worker:

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
| `text_embeddings` | Vector embeddings from an HTTP embedding server you configure — self-hosted or a paid API both work. See `[embeddings]` in config.toml for worked examples |
| `vision_query` | Ask a question about an image via your own vision-capable chat server, instead of sending it to Anthropic's API. See `[vision]` in config.toml for worked examples |
| `speak` · `listen` | Local text-to-speech (Piper) and speech-to-text (faster-whisper) through your own speaker/mic — no cloud audio API. Disabled by default; see `[speak]`/`[listen]` in config.toml, including first-time device setup |
| `<worker>__delegate` | Hand a whole task to a ResearchMesh agent on another machine |

Every machine has its own copy of all this. The `python` kernel here is not a
worker's kernel, and `/memories` here is not a worker's memory store. Same names,
different computers, no shared state.

## Good to know

- One request can fan out into many tool calls, local and worker alike (capped at 75
  per turn).
- **A down worker and a mistyped worker name look identical to `/workers` and
  `/dagent`.** Both are simply "not there this turn" — `/workers` deliberately only
  reports what's actually reachable right now (a cached listing could report a
  machine that went down ten minutes ago), so a rejected name could be either.
- **Why `/dagent` exists.** A local `bash` is instant; a `delegate` takes minutes and
  has to be written as an outcome. Left alone, Claude prefers the local tool and
  quietly does a worker's job on the wrong machine. `/dagent` removes the local tools
  from the request, so it can't.
- **If it starts returning 400s and won't stop, run `/clear`.** Two failures persist
  for the life of the process — an unanswered `tool_use` block, and a conversation
  past the context window — and both make every later turn fail identically. The
  error report names which one you hit; `/clear` recovers from either without
  dropping your worker connections.
- **`ruff check .` and `mypy .` should both pass.**
- **`python smoke_test.py` before you commit.** No API key, no network, no running
  workers — it builds a fleet of fakes and asserts the things that break *silently*:
  that two workers exposing the same tool name get two distinct, API-legal names;
  that the namespacing round-trips; that a dead worker is skipped instead of taking
  the fleet down; that groups fan out while one worker's calls stay serial; that every
  `tool_use` block gets exactly one `tool_result`, in order; and that `/dagent` really
  withholds every local schema.
- **`python e2e_test.py` is a fourth check, kept out of the gates because it spends
  real tokens** (~15s, needs `ANTHROPIC_API_KEY`). It launches two workers over real
  stdio MCP and covers what fakes can't: that the duplicate-name 400 is genuinely the
  API's behavior, that namespacing survives a real transport, and that a real model
  issues both calls in one turn.

<a id="setup-linux"></a>

## Setup (Linux)

You need **Linux**, **Python 3.11+**, and an Anthropic **API key** — this is an API
client, so a Claude subscription won't work.

### 1) Install system packages and create a venv

```bash
sudo apt install python3 python3-venv python3-dev build-essential \
                 libreoffice pandoc python3-tk scrot

python3 -m venv ~/researchmesh-router
source ~/researchmesh-router/bin/activate
pip install -r requirements.txt
```

`libreoffice` + `pandoc` back `document_convert`; `python3-tk` and `scrot` back
`computer` — see step 3. Router needs the same backings as
[ResearchMesh](https://github.com/nodormu/ResearchMesh) itself, since it executes the
same local tools in addition to delegating — every per-tool package is installed
unconditionally via `requirements.txt`, none of them are meant to be skipped, and each
is only *imported* lazily, at the moment its tool actually runs.

### 2) Playwright

```bash
playwright install chromium            # the browser binary — pip installs the package, not this
sudo playwright install-deps chromium  # OS libraries
```

### 3) `computer` — extra apt packages, and X11 vs Wayland

`pip install pyautogui` succeeds on its own, so a missing-package failure here is
misleading — `computer` reports `pyautogui` as missing when it's really one of these
two apt packages: **`python3-tk`** (`pyautogui` pulls in `mouseinfo`, which imports
`tkinter` at module level) or **`scrot`** (`pyscreeze`'s screenshot path on X11).

`computer` also needs a real **X11** display — it synthesises input via X11/XTEST,
which Wayland compositors ignore by design, so it refuses up front on a Wayland
session (check `echo $XDG_SESSION_TYPE`) instead of clicking into the void:

```bash
# 1. Log in to an "Xorg"/"X11" session at your display manager, or
# 2. Run the whole client inside a nested X server:
sudo apt install xvfb
xvfb-run -s '-screen 0 1280x800x24' python main.py
# 3. XWayland-only setup and you want to try regardless:
export CLAUDE_COMPUTER_FORCE=1
```

### 4) Environment variables

```bash
export ANTHROPIC_API_KEY=sk-ant-...           # add to ~/.bashrc to keep it
export CLAUDE_MEMORY_DIR=~/.router-memories   # else it writes into this repo
```

**Set `CLAUDE_MEMORY_DIR` explicitly.** The default (`./memories`, relative to the
working directory) is the same default ResearchMesh itself uses — if you ever run
both from adjacent checkouts or the same parent directory, they'd otherwise write
into two different `./memories` paths that only look related, not one shared or
namespaced store. Pointing Router at its own path (`~/.router-memories` or similar)
avoids the ambiguity entirely.

### 5) Run it

```bash
python main.py
```

**Workers are optional** — `config.toml` ships with every server commented out, so a
fresh clone runs on the 22 local tools alone.

### 6) Using it

Just type. At the `>` prompt:

| | |
|---|---|
| `<anything>` | ordinary turn — local tools *and* workers are offered |
| `/workers` | list the workers that are up |
| `/dagent <task>` | delegate-only: the local tools are withheld for this turn |
| `/dagent <worker> <task>` | the same, pinned to one machine |
| `/think <anything>` | give Claude longer to reason |
| `/clear` | drop the conversation, keep the workers connected |
| `/voice [on\|off]` | toggle whether Claude's replies also get spoken aloud (`speak`, local Piper TTS) |
| `/listen [N]` | record `N` seconds from your mic (or `[listen].default_duration_seconds`), transcribe locally (faster-whisper), and auto-submit it as your next turn — no Enter press needed, works the same whether `/voice` is on or off |

`/voice`/`/listen` require `[speak]`/`[listen]` set up in `config.toml` first (see the
tools table above and that file's own inline setup comments) — both are shipped fully
commented out, same as `[vision]`/`[embeddings]`. Without that, `/voice` still toggles
but has nothing to speak, and `/listen` reports a clear `not_configured`/`disabled`
message instead of trying to open the mic.

**Ctrl-C** exits and shuts everything down cleanly.

## Configuration

Non-secret settings live in `config.toml`. Secrets stay in the environment — the app
does **not** read a `.env` file. **Workers are optional** — it ships with every server
commented out, so a fresh clone runs on the 22 local tools alone.

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

A worker that's unreachable prints a warning and is skipped, so one being down
doesn't stop the app. Tokens are never stored in `config.toml`, which is committed —
only the *name* of the variable that holds one. Generate a token with
`python -c "import secrets; print(secrets.token_urlsafe(32))"`.

See [Adding workers](#adding-workers) below for what each field does and how to bring
a worker online in the first place.

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Read from the shell. The app does not load a `.env`. |
| `CLAUDE_MODEL` | Overrides `[claude] model` in `config.toml`. |
| `CLAUDE_MEMORY_DIR` | Where `memory` stores `/memories`. Defaults to `./memories` **relative to the working directory** — set it (see [Setup](#setup-linux) step 4). |
| `CLAUDE_SHOW_USAGE=1` | Per-request token and prompt-cache counters. |
| `CLAUDE_KERNEL_ENCRYPTION` | `auto` (default) encrypts the local `python` kernel's ZeroMQ sockets with CurveZMQ, falling back if the installed versions can't; `required` fails the tool rather than running unencrypted; `off` skips it. Covers *this* machine's kernel only — a worker's kernel reads the variable from the worker's own environment. |
| *(per worker)* | Each `token_env` names the variable holding that worker's bearer token. No `token_env` means unauthenticated. |
| *(embeddings server)* | Whatever `[embeddings].api_key_env` names, if your server needs auth. |
| *(vision server)* | Whatever `[vision].api_key_env` names, if your server needs auth. |

<a id="adding-workers"></a>

## Adding workers

Any MCP server works — ResearchMesh is just what it was built and tested against.
Each worker's tools are prefixed with its `name`, so identical machines never
collide.

On each worker machine, run ResearchMesh as a server:

```bash
export RESEARCHMESH_MCP_TOKEN=...
python mcp_server.py --transport streamable-http --host 0.0.0.0 --port 8100
```

Then add it to `config.toml` here — see [Configuration](#configuration) above for the
full example. Four fields deserve a second look.

**`name` becomes the tool prefix** — `gpu-box__delegate`. Keep it short; letters,
digits, `_` and `-` only. Anything else is substituted with `_`, so `gpu box` and
`gpu-box` would collide.

**`description` is strongly recommended.** ResearchMesh hardcodes a single
description constant, so every worker describes itself identically unless you add
your own. Write what's true of *that* machine: its OS and session type, what's
installed, what's attached, what data is on it, what it must not be used for. It's
prepended to that worker's tools as `[worker: name] ...` and makes routing choices
much more reliable.

**`timeout_seconds` matters more than it looks.** The MCP SDK defaults to 300s. A
worker driving a GUI runs longer than that, and when the timeout fires the work is
already done on the far side and simply lost. Default here is 900s; raise it per
worker for long compute. The *connect* timeout stays at 15s, so a machine that's
switched off fails in seconds instead of hanging the turn.

**`url` can be `https://`.** The router does not add custom certificate logic; it
uses the normal HTTP client trust configuration for the runtime. A company CA or
private certificate therefore works only if that CA is already trusted on the client
machine, or if `SSL_CERT_FILE=/path/ca.pem` / `SSL_CERT_DIR=/path/to/certs` is set
for that process. The certificate itself belongs on the *worker* side
(`mcp_server.py --ssl-certfile/--ssl-keyfile`). Over plain `http://` the bearer token
and every task and result cross the network in the clear, which is fine on a trusted
LAN and is not on a corporate one.

**How work is distributed.** Worker calls are grouped by machine. Groups run
concurrently; calls within a group run in order — because a ResearchMesh worker has
one mouse, one browser page and one kernel, and serialises `delegate` behind a lock.
So `max_parallel` is really "how many machines at once." Local tools run in order
for the same reason. A turn calling three workers takes as long as the slowest one,
not the sum.

<details>
<summary><b>Project layout and extending</b></summary>

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
  text_embeddings.py  vision.py  speak.py  listen.py
```

Adding a **worker** is a config edit, no code. Adding a **local tool** is one module
exposing `TOOLS`/`handles()`/`execute()`, plus a line in `local_tools.py`.

</details>

## Recommended local tools (optional — saves tokens)

None of these are dependencies — nothing here breaks without them. They're suggested
purely so Claude reaches for a fast, purpose-built local binary via `bash` instead of
burning tokens re-implementing the same job in `python`, or reading whole files through
the file editor just to search them. Install whichever are useful to you; skip the rest.
Everything below is `apt`/`snap`/`flatpak`, or (for Rust) the official `rustup`
installer — commands as written are Debian/Ubuntu-specific. On another distro, the
tool names are the same; swap in your own package manager (`dnf`, `pacman`, `zypper`,
etc.) yourself. `apt`/`flatpak` lines include `-y` since Claude may run these itself via
`bash`, which has no terminal for either to prompt against; drop it if running by hand
and you'd rather review each one first.

**This is the router's own machine only.** Each worker is a separate ResearchMesh
install with its own `bash`, its own filesystem, its own set of these tools or lack
thereof — installing something here doesn't make it available on `gpu-box` or
`scraper`. Repeat whatever's useful on each worker machine directly.

```bash
# --- Search, text & structured data -----------------------------------------------
sudo apt install -y ripgrep       # rg — recursive search, instead of reading whole files to grep them
sudo apt install -y fd-find       # fd — fast, .gitignore-aware find. NOTE: the binary is `fdfind`,
                                # not `fd` (Debian name clash with an unrelated package)
sudo apt install -y bat           # cat with syntax highlighting + line numbers. NOTE: the binary is
                                # `batcat`, not `bat` (same kind of Debian name clash as fd-find)
sudo apt install -y jq            # jq — query/reshape JSON from the shell
sudo apt install -y yq            # yq, but for YAML. NOTE: Debian's `yq` is the OLD Python
                                # jq-wrapper-for-YAML (`yq '.filter' file.yaml`), NOT the popular
                                # Go-based mikefarah/yq most online docs assume (`yq e '.path' file`)
sudo apt install -y miller         # mlr — CSV/TSV/JSON reshape/filter/stats from the shell
sudo apt install -y fzf            # fuzzy finder; use `--filter` for non-interactive/scripted matching

# --- File search & disk usage ------------------------------------------------------
sudo apt install -y plocate        # modern `locate` — instant filename search across the whole disk,
                                 # from a background-updated index (run `sudo updatedb` once first)
sudo apt install -y tree           # directory-structure dumps
sudo apt install -y ncdu            # interactive, curses-based disk usage — see what's eating space
sudo snap install dust           # fast, visual `du` — not in the default apt repos, snap only
sudo apt install -y duf             # nicer `df`, disk-space-by-volume at a glance

# --- Archives & binary inspection ---------------------------------------------------
# tar/gzip already exist on every Debian/Ubuntu system (Essential: yes — no install
# possible even if you wanted to skip them), and zip/unzip/xz-utils ship as part of the
# standard Ubuntu task. Between those four, "basically every format" is already covered
# before you install anything — unlike Windows, which has no built-in CLI archiver at
# all. The one real gap:
sudo apt install -y unrar            # RAR extraction — the one common format Linux has
                                   # nothing built in for (RAR itself is proprietary)
# 7-Zip's own .7z format is the other thing genuinely missing — worth adding only if you
# actually receive .7z files, not as a general-purpose necessity:
sudo apt install -y 7zip             # NOTE: this used to be `p7zip-full` — that package no
                                   # longer exists on current Ubuntu, replaced by the
                                   # upstream-maintained `7zip` package (still gives `7z`)
sudo apt install -y hexyl            # colorized hex+ASCII dump, e.g. for raw SysEx/firmware bytes
sudo apt install -y binwalk          # scans a binary for embedded file signatures/firmware images —
                                   # the closest apt-packaged equivalent to a deep file-type identifier

# --- Git / GitHub / diffing ---------------------------------------------------------
sudo apt install -y gh              # GitHub CLI — PRs/issues/releases from the shell
sudo apt install -y git-delta       # syntax-highlighted, side-by-side git diff pager. NOTE: the plain
                                  # `delta` apt package is a DIFFERENT, unrelated 2006 tool and
                                  # installs no `delta` binary at all — `git-delta` is the one that
                                  # actually provides the `delta` command

# --- HTTP / API testing --------------------------------------------------------------
sudo apt install -y httpie          # much more readable than raw curl for poking at APIs. NOTE: the
                                  # request-sending command is `http`, not `httpie` — the bare
                                  # `httpie` command is a separate plugin-manager subcommand

# --- C / C++ / Rust toolchains --------------------------------------------------------
# gcc/g++/make (build-essential) are already installed if you followed Setup step 1 —
# nothing missing there. clang is a genuine alternative compiler worth having on top:
sudo apt install -y clang            # self-contained C/C++ compiler, alternative to gcc
sudo apt install -y cmake            # build system generator
sudo apt install -y ninja-build      # fast build backend, pairs with cmake
# Rust: use the official rustup installer, not a distro package — apt's rustc/cargo lag well
# behind upstream and can't be updated independently of the whole system:
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh

# --- System diagnostics ---------------------------------------------------------------
# strace and lsof are already on any standard Ubuntu install (both are part of the
# `ubuntu-standard` task) — nothing to add there, they're just worth knowing about:
# `strace <cmd>` traces a process's syscalls (first move for "why is this hanging"),
# `lsof` shows what has a given file/port open.
sudo apt install -y htop            # interactive process viewer, nicer than plain `top`
sudo apt install -y procs           # modern `ps` replacement, colorized/tree-aware output
sudo apt install -y hyperfine       # benchmarking — compare two commands' real run time

# --- Audio production & media metadata ------------------------------------------------
sudo apt install -y ffmpeg                    # ffmpeg/ffprobe — audio/video transcoding and inspection
sudo apt install -y sox                       # CLI audio conversion/trim/resample, complements ffmpeg
sudo apt install -y mediainfo                 # instant codec/bitrate/duration metadata
sudo apt install -y libimage-exiftool-perl    # exiftool — metadata on images/audio/PDFs/almost anything
                                            # (package name differs from the `exiftool` command it installs)

# --- Images & graphic design -----------------------------------------------------------
sudo apt install -y imagemagick     # convert/mogrify/compare — image conversion & editing from the shell
sudo apt install -y krita           # digital painting/illustration, distinct from GIMP (raster) and
                                  # Inkscape (vector)
sudo apt install -y webp            # cwebp/dwebp — encode/decode the WebP image format from the shell

# --- Video editing -----------------------------------------------------------------------
sudo apt install -y handbrake-cli   # video transcoding with sane presets, complements ffmpeg
sudo flatpak install -y flathub org.shotcut.Shotcut   # free timeline-based video editor, not
                                                     # reliably in the default apt repos
# DaVinci Resolve (the other obvious free NLE) has no apt/snap/flatpak package — Blackmagic
# only distributes it via a manual download + free account signup from their own site.

# --- Documents & writing -----------------------------------------------------------------
sudo apt install -y poppler-utils   # pdftotext/pdftoppm/pdfinfo/pdfimages — pull just the pages you
                                  # need out of a PDF as text, without going through LibreOffice
sudo apt install -y calibre         # ebook-convert (CLI) — epub/mobi/azw3/etc., more formats than
                                  # document_convert reaches
sudo apt install -y hunspell        # command-line spell-checking
```

## Origin

The CLI shell, Anthropic wrapper and MCP client began as copies from
[ResearchMesh](https://github.com/nodormu/ResearchMesh) (same author, MIT); the
sixteen tool modules were copied later, verbatim. `diff -rq --exclude=__pycache__
../ResearchMesh/core core` should show only `chat.py`, `claude.py`, `tools.py` and
`cli.py` — anything else is drift. A fix to a tool in either repo should be a
straight `cp`.

It exists because a plain MCP bridge passes tool names through verbatim, so three
ResearchMesh workers all advertising `delegate` get rejected outright
(`400 ... Tool names must be unique`). [core/tools.py](core/tools.py) is the fix.
**If Claude Code is your front end you don't need any of this** — it already
namespaces MCP tools as `mcp__<server>__<tool>`.

## Not built yet

- **No loop protection.** Nothing stops a worker's config pointing back here.

## License

[MIT](LICENSE) — use it, fork it, ship it. No warranty; see the file for the full text.
