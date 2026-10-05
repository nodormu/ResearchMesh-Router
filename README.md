# ResearchMesh-Router

> *Unofficial, community-built client — not affiliated with or endorsed by Anthropic. "Claude" is a trademark of Anthropic.*

                                  ┌── Bash / Filesystem
                                  ├── Playwright / LibreOffice
                                  ├── Python kernel / DuckDB
        ResearchMesh-Router ──────┤
                                  ├── workstation ── ResearchMesh (Agent)
                                  ├── gpu-box ────── ResearchMesh (Agent)
                                  └── scraper ────── ResearchMesh (Agent)

The **same toolset as [ResearchMesh](https://github.com/nodormu/ResearchMesh)**,
plus the ability to drive any number of ResearchMesh agents on other machines,
without the tool-name conflicts that combination normally causes.

Two kinds of tool, in one list:

- **Local** — `bash`, `python`, `computer`, `memory`, `browser_navigate`, … run
  here, immediately.
- **Worker** — `gpu-box__delegate`, `scraper__delegate`, … run on another
  machine, over MCP.

Ask for something and Claude picks the machine. Independent work on different
workers runs at the same time.

**Not an MCP server, by design.** It connects *out* to ResearchMesh workers or
other MCP servers; nothing connects *in*, which keeps tool names unambiguous.
Claude Code can reach each worker directly but cannot drive the whole mesh
through one endpoint, and this router cannot be a worker in someone else's
fleet.

**Less restrictive than Claude Code.** No approval prompts, no permission model,
no context compaction. It runs any program, command or script your user can
run, on this machine and on every worker. That is the point, and the risk.

**Scope each worker like a role-scoped employee account, not one all-access
account.** No approval gating exists anywhere in this fleet, locally or on any
worker. The mitigation is OS-level access matched to the machine's job: a
dedicated non-admin account, file and directory permissions, GPOs or
Configuration Profiles (see
[ResearchMesh](https://github.com/nodormu/ResearchMesh)'s README for the Linux,
Windows and Mac mechanisms). A misrouted or hallucinated request then fails at
the OS layer: a "Graphic Designer" worker asked to modify a production database
cannot, because its account has no database access. Scope each worker's account
to its `description` in `config.toml`, not to whatever is convenient to set up.

**Workers as employees.** Beyond OS scoping, give each worker its own email
address, let it work with people and other AIs in Teams or Slack, and route its
work through the systems everyone else uses: a CRM/CMDB such as ServiceNow or
ConnectWise as the system of record, change tickets for anything that touches
production. None of this is built into the 26 local tools;
[Adding workers](#adding-workers) connects a worker to an email, Teams/Slack or
CMDB MCP server, and it participates through the same front doors a new hire
would. "No approval gating" means no y/n dialog in this software, not that
nothing gates a risky change: a maintenance request can be submitted instantly,
but whether it runs depends on the same Change Advisory Board approval a
human's request needs, because that gate lives in the change-management process,
not in this client.

## What it can do

**26 local tools**, plus one per connected worker:

| Tool | For |
|---|---|
| `bash` | Shell commands as your user via `/bin/bash` by default. Stateless: a fresh subprocess each call. `[bash]` in config.toml selects a different shell (e.g. zsh) |
| `str_replace_based_edit_tool` | View, create, and edit files |
| `web_search` · `web_fetch` | Anthropic's server-side search and page fetch |
| `memory` | A `/memories` store that **persists across sessions** — the only state that outlives the process |
| `computer` | Screenshots plus mouse/keyboard control, on X11 (`pyautogui`) or Wayland (xdg-desktop-portal remote control; needs `dbus-next` and `spectacle` or `grim`) |
| `desktop_window` | List windows, and focus, move, resize, full-screen, minimize or restore one, on a KDE desktop (KWin scripting; needs `dbus-next`), so keystrokes reach the right window |
| `screen_find` | Find on-screen text (`text`) or button-like blocks (`buttons: true`), limited to a `region` if given, by OCR, and return click coordinates in `computer`'s space; reads text on coloured buttons that plain OCR misses (needs `tesseract`) |
| `browser_navigate` · `_links` · `_click` · `_fill` · `_extract` · `_back` · `_tab` | [Playwright](https://playwright.dev/) DOM browsing: renders JavaScript, follows links and new tabs, fills forms, saves downloads to `~/Downloads`. `_navigate` takes `mode` and `profile` (see below), `_tab` lists, switches and closes tabs, and `_fill` takes a `pass` vault entry (`value_secret`) without the value appearing in the conversation, or `submit` to press Enter afterwards |
| `document_convert` | LibreOffice + pandoc. Markdown → `.docx`/`.odt`/`.pdf`, or any office format to any other |
| `python` | Persistent IPython kernel — **variables survive between calls** |
| `bash_session` | Persistent shell — **cd/env/venvs/background jobs survive between calls** |
| `interactive_run` | Commands that prompt: passwords, `[y/N]`, ssh host keys, installers |
| `config_edit` | Edit YAML/TOML/JSON **without destroying your comments** |
| `sql_query` | DuckDB straight against CSV/Parquet/JSON — no import step |
| `trash` | Recoverable deletes instead of `rm` |
| `text_embeddings` | Vector embeddings from an HTTP embedding server you configure, self-hosted or a paid API. See `[embeddings]` in config.toml for worked examples |
| `vision_query` | Ask a question about an image via your own vision-capable chat server, instead of sending it to Anthropic's API. See `[vision]` in config.toml for worked examples |
| `speak` · `listen` | Local text-to-speech (Piper) and speech-to-text (faster-whisper) through your own speaker and mic; no cloud audio API. Both return `not_configured` until `[speak]` and `[listen]` are set in config.toml, which also covers first-time device setup |
| `<worker>__delegate` | Hand a whole task to a ResearchMesh agent on another machine |

**Browser modes.** `browser_navigate` takes `mode` and `profile`:

- `headless` (default): no window; installed Chrome if present, else bundled Chromium.
- `headed`: a visible window on your desktop.
- `virtual`: Chrome on a hidden display (Xvfb); no window appears.
- `real`: your installed Chrome started normally and attached over CDP, the least
  detectable mode. It opens a window you can click in.
- `profile` names a persistent profile (cookies and logins survive restarts) under
  `~/.cache/researchmesh/browser-profiles`. Without one the session is temporary.
  Changing mode or profile restarts the browser.
- A report carries a `Human check:` line when a Cloudflare check appears. A fresh
  default-mode visit that a check stops is reopened once in `virtual` mode; if it
  still says pending, use `real` or click the check yourself.

Every machine has its own copy of all this. The `python` kernel here is not a
worker's kernel, and `/memories` here is not a worker's memory store. Same names,
different computers, no shared state.

## Good to know

- One request can fan out into many tool calls, local and worker alike (capped at
  200 per turn).
- **A down worker and a mistyped worker name look the same** to `/workers` and
  `/dagent`. `/workers` reports only what is reachable right now (a cached
  listing could report a machine that went down ten minutes ago), so a rejected
  name could be either.
- **`/dagent` exists because a local `bash` is instant, while a `delegate` takes
  minutes and has to be written as an outcome.** Left alone, Claude prefers the
  local tool and does a worker's job on the wrong machine. `/dagent` removes the
  local tools from the request.
- **If it keeps returning 400s, run `/clear`.** Two failures persist for the life
  of the process, an unanswered `tool_use` block and a conversation past the
  context window, and both fail every later turn the same way. The error report
  names which one you hit; `/clear` recovers from either and keeps your worker
  connections.
- **`ruff check .` and `mypy .` should both pass.**
- **Run `python smoke_test.py` before you commit.** It needs no API key, network
  or running workers. It builds a fleet of fakes and checks what breaks silently:
  two workers exposing the same tool name get two distinct, API-legal names; the
  namespacing round-trips; a dead worker is skipped; groups fan out while one
  worker's calls stay serial; every `tool_use` block gets exactly one
  `tool_result`, in order; `/dagent` withholds every local schema; and the docs
  match the code (README's tool count, CLAUDE.md's module list).
- **Two checks sit outside the gates because they spend real tokens.**
  `python e2e_test.py` (~15s, needs `ANTHROPIC_API_KEY`) launches two workers
  over real stdio MCP and checks that the duplicate-name 400 is the API's actual
  behavior, that namespacing survives a real transport, and that a real model
  issues both calls in one turn. `python test_model_compat_live.py` (real API,
  ~9 requests) checks the per-model tool-compatibility handler against
  Anthropic's actual error wording.

<a id="setup-linux"></a>

## Setup (Linux)

You need **Linux**, **Python 3.11+**, and an Anthropic **API key**. This is an API
client, so a Claude subscription won't work.

### 1) Install system packages and create a venv

```bash
sudo apt install python3 python3-venv python3-dev build-essential \
                 libreoffice pandoc python3-tk scrot pulseaudio-utils \
                 tesseract-ocr xvfb

python3 -m venv ~/researchmesh-router
source ~/researchmesh-router/bin/activate
pip install -r requirements.txt
```

`libreoffice` and `pandoc` back `document_convert`; `python3-tk` and `scrot` back
`computer` (step 3); `tesseract-ocr` backs `screen_find`; `xvfb` backs the
browser's `virtual` mode and the nested X server in step 3. `pulseaudio-utils`
backs `speak` and `listen` (`paplay`, `parecord`), which call it directly with no
fallback, so a missing package is a raw subprocess failure, not a tool that
declares itself unavailable. A desktop with PipeWire usually has it; a headless
server or WSL does not.

The router needs the same backings as
[ResearchMesh](https://github.com/nodormu/ResearchMesh) because it runs the same
local tools. Every per-tool package in `requirements.txt` is required, and each
is imported lazily, when its tool first runs.

### 2) Playwright

```bash
playwright install chromium            # the browser binary — pip installs the package, not this
sudo playwright install-deps chromium  # OS libraries
```

### 3) `computer` — extra apt packages, and X11 vs Wayland

`pip install pyautogui` succeeds on its own, so a missing-package failure here is
misleading: `computer` reports `pyautogui` as missing when it is really one of two
apt packages. **`python3-tk`**: `pyautogui` pulls in `mouseinfo`, which imports
`tkinter` at module level. **`scrot`**: `pyscreeze`'s screenshot path on X11.

`computer` works on **X11** (`pyautogui`) and on **Wayland** (`echo
$XDG_SESSION_TYPE`). On Wayland it goes through xdg-desktop-portal: the desktop
may ask for approval when a session starts, and while it lasts KDE shows a
"Remote Control" tray icon whose **End** entry stops it (the next action starts a
new session). It needs `dbus-next` (in `requirements.txt`) and `spectacle` or
`grim` for screenshots. The screen is one monitor: the leftmost one shared in the
dialog, or `CLAUDE_COMPUTER_MONITOR=<index>`. Share every monitor in the dialog.
With only some shared, the screenshot scale is estimated (exact when they span the
desktop's width or height) and a warning is printed.
`CLAUDE_DISPLAY_SIZE=WxH` sets the logical display size declared to the model
(default `1280x800`). Screenshots go to the model, as for any use of this tool.
To use X11/XTEST on an XWayland-only setup or inside a nested X server instead:

```bash
xvfb-run -s '-screen 0 1280x800x24' python main.py   # nested X server
export CLAUDE_COMPUTER_FORCE=1                         # XWayland-only setup
```

### 4) Environment variables

```bash
export ANTHROPIC_API_KEY=sk-ant-...           # add to ~/.bashrc to keep it
export CLAUDE_MEMORY_DIR=~/.router-memories   # else it writes into this repo
```

If you also use Claude Code with a subscription, add this alias too (same file) so
the API key doesn't shadow your subscription auth:

```bash
alias claude='env -u ANTHROPIC_API_KEY claude'
```

**Set `CLAUDE_MEMORY_DIR` explicitly.** The default is `./memories`, relative to
the working directory, and ResearchMesh uses the same default. Run both from
adjacent checkouts and they write to two different `./memories` paths that only
look related. Point the router at its own path (`~/.router-memories` or similar).

`RESEARCHMESH_DOWNLOAD_DIR` changes where browser downloads land (default
`~/Downloads`).

### 5) Run it

```bash
python main.py
```

`config.toml` ships with `[mcp].enabled = false`, so a fresh clone runs on the 26
local tools alone. Set it to `true` once you have added workers.

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
| `/model` | list the ROUTER's own models, `/model swap <name\|index>` to swap for this session |
| `/model <worker>` | list a CONNECTED WORKER's models, `/model <worker> swap <name\|index>` to swap them remotely |
| `/voice [on\|off]` | toggle whether Claude's replies also get spoken aloud (`speak`, local Piper TTS) |
| `/listen [N]` | record `N` seconds from your mic (or `[listen].default_duration_seconds`), transcribe locally (faster-whisper), and auto-submit it as your next turn — no Enter press needed, works the same whether `/voice` is on or off |

`/voice` and `/listen` need `[speak]` and `[listen]` set in `config.toml` first
(see the tools table above and that file's inline setup comments). Both ship
fully commented out, like `[vision]` and `[embeddings]`. Without that, `/voice`
still toggles but has nothing to speak, and `/listen` reports `not_configured` or
`disabled` instead of opening the mic.

**`/model`** lists the models in `config.toml`'s `[claude] claude_models`, each
with an index. **`/model swap <name or index>`** swaps the model for this session
only; it never edits `config.toml`, so a new session starts on the first entry.
The list is a live-refreshed cache, not hand-typed: about once a day
(`model_scan_ttl_hours`, default 24) it re-scans Anthropic's `/v1/models` and
rewrites `claude_models` to one entry per model family, newest first, sonnet
first when present. A failed scan (offline, bad key) changes nothing on disk.
**This affects only the router's own reasoning model.** A connected worker's
model is set in that worker's own `config.toml`.

**Haiku 4.5 has no `computer` tool.** It rejects it, so the client drops the tool
for Haiku after one rejected request (a `[model compat]` line is printed) and
every other tool keeps working. If `computer` was used earlier in the
conversation on another model, `/model swap` to Haiku fails every turn with a 400
(`toolset_name 'computer' on a tool_use block is not the family of a declared
toolset entry (no toolset entry is declared)`): swap back, or `/clear`.

**`/model <worker>`** lists a connected worker's models instead, e.g.
`/model gpu-box`, from that worker's own `model` MCP tool (a sibling of
`delegate`): no agent turn is spent and no Anthropic API call is made.
**`/model <worker> swap <name or index>`** swaps that worker's model immediately,
for every later `delegate` call to it from any session, until changed again or
that worker process restarts. Every worker response is printed with a
`[worker: <name>]` prefix, never a bare `[model: ...]`, so it cannot be mistaken
for the router's own `/model` output. The worker owns its own TTL and live-scan
cache; the router adds no TTL logic to a remote call. The slash command is not
required: the router's own Claude can see and call a connected worker's `model`
tool during a normal turn, since it is namespaced into the tool list like
`delegate`, so asking in plain language ("swap gpu-box to opus") works too.

**Ctrl-C** exits and shuts everything down cleanly.

### 7) Test it

Each prompt below is meant to be pasted into the CLI as is.

a) **Build your own persistent memory of this machine — do this one first, always.**
```
Before we do anything else, I want you to build yourself some persistent memory about
this machine, since /memories is the only state that survives a session reset or a
restart — everything else (the Python kernel, the browser page, the DuckDB connection)
resets every time. Figure out what Linux distro and version this actually is first
(don't assume — check `/etc/os-release`, `uname -a`, etc.), then scan this machine's
real hardware (CPU, RAM, GPU, disks) and what's actually installed: CLI tools on PATH
via `command -v`, packages via whichever package manager this distro actually uses
(`dpkg`/`apt` on Debian/Ubuntu, `rpm`/`dnf` on Fedora, `pacman` on Arch, `zypper` on
openSUSE, etc. — check which one applies here rather than guessing), plus snap/flatpak
if either is present. Then write two files: 01_environment_notes.md (hardware specs,
the distro/OS version you actually found, disk layout, and any quirks or behaviors you
run into along the way — display server, privilege model, which package manager(s) are
in play) and 01_system_tool_inventory.md (a categorized inventory of what's already
installed — GUI apps, CLI tools, dev-assistant tools, reusable scripts you find lying
around — so you reach for a real local tool instead of writing something from scratch
every time). In both files, add a short instruction near the top telling your future
self to re-scan and refresh the file's contents the next time you're asked to read them,
rather than trusting old data blindly — so this stays accurate as things change on this
machine over time.
```
NOTE: this is the most useful prompt on the list. Do it once and every later session
starts already knowing your machine. It builds this machine's own memory; a worker you
add later builds its own.

b) **List its own slash commands.**
```
List all your custom commands and their options.
```

c) **Understand why any of this is worth doing.**
```
Now that you've looked at what's installed on my machine, explain in plain terms why
it's worth installing extra local command-line tools — like ripgrep, fd, jq, ffmpeg,
ImageMagick — instead of just having you write a one-off script from scratch every
time I ask for something similar. What's actually being saved by doing this?
```

d) **Install the recommended tools, one at a time.**
```
Look at the "Recommended local tools" section further down in this project's
README.md, and install every tool listed there via apt/snap/flatpak/rustup — one at
a time. Wait for each install to fully finish and tell me whether it succeeded or
failed before starting the next one. Don't batch them together.
```
NOTE: this installs on this machine only (the router). Repeat it on each worker.

e) **Mouse/keyboard GUI control.**
```
Open a text editor (gedit, kate, or whatever opens by default), type "Hello, I am
controlling your mouse and keyboard," save it to my Desktop, then export that same
file as a PDF, also saved to my Desktop.
```
TIP: don't touch your mouse or keyboard while it runs; fighting it for control makes
the task harder. On Wayland the desktop may ask for approval (step 3).

f) **Headless, DOM-based web browsing.**
```
Go to news.ycombinator.com using DOM-based browsing — not a visible browser window —
open the #1 story on the front page, and give me a short summary of it.
```
NOTE: this reads and surfs the web without opening a window or touching your mouse
and keyboard.

g) **Write a document, then convert it.**
```
Write a short one-page markdown file about the history of the QWERTY keyboard layout,
then convert it to a PDF and save both the markdown and the PDF to my Desktop.
```

h) What is the airspeed velocity of an unladen swallow?

All of the above run on this machine and need no worker. Once you have added one
(see [Adding workers](#adding-workers)), try `/workers` and `/dagent`.

### 8) interactive_run — log in without Claude seeing your passwords

`interactive_run` answers a command's prompts (sudo, ssh, git, anything that asks
for a password) from a vault on your machine. The model supplies only the name of
an entry; the value is decrypted locally and never appears in the conversation.
`browser_fill` takes the same vault entries for web logins (`value_secret`). Set
up the vault once (below). After that, whenever a command needs a credential, the
agent asks you to pick from the names you saved.

**Name check.** An entry is decrypted only if you typed its name in one of your
own messages this session, so the model cannot pick one on its own. When it needs
a credential it lists the real entry names and waits for you to name one. A typed
name stays confirmed for the rest of the session and for any use. The match is on
the whole name anywhere in your message, so a passing mention ("push it to github"
with an entry named `github`) also confirms it.

**What is and is not protected:**

- The value goes from `pass show` to the child process over a pty and is never in
  a tool call. The transcript returned to the model has the value scrubbed, along
  with its percent, form, HTML, JSON, hex and base64 encodings. A reversed or
  otherwise transformed copy that the child prints is not caught and would reach
  Anthropic.
- sudo's password feedback (asterisks) shows the password's length in the
  transcript, not its text.
- `send_env` takes the NAME of an environment variable and is scrubbed the same
  way, without `pass`.
- `browser_fill` types a confirmed entry into whatever page is open. A malicious
  page that talks the model into filling its login form receives the real value,
  and scrubbing does not help, because the value never returns through the model.
  Name an entry only when you want it used, and watch which site the browser is on.
- Only the first line of a `pass` entry is used.
- A GPG passphrase prompt (`pinentry`) appears on your screen, not in the
  conversation. If the key is not cached and nobody answers, `pass show` times
  out after 30 s; unlock the key once in your own terminal first.
- `computer` has no vault option: type a password into a native window yourself.
- A one-time code (authenticator, SMS, email) is not a vault secret. Paste it in
  the chat and the agent enters it at once with `browser_fill` `submit: true`.

<details>
<summary><strong>Full <code>pass</code> vault setup, walkthrough + reference charts (click to expand)</strong></summary>

**One-time `pass` setup — install first:**
```
sudo apt install pass pinentry-curses
```

```
SETUP SEQUENCE SETTING UP A VAULT FROM SCRATCH
══════════════════════════════════════════════

Step 1: gpg --full-generate-key
  You type:   Name, Email, Passphrase
  Purpose:    Creates your encryption key (a public/private key pair)

Step 2: gpg --list-secret-keys
  You type:   Nothing — just run it
  Purpose:    Shows you the Key ID (long hex string) you'll need next

Step 3: pass init <key-id>
  You type:   The Key ID from step 2
  Purpose:    Tells pass "encrypt my whole vault using this key"

Step 4: pass insert <entry-name>
  You type:   A name you choose, then the secret value to store
  Purpose:    Encrypts and saves one password under that name

Step 5: pass show <entry-name>
  You type:   Nothing — just the entry name
  Purpose:    Decrypts and prints that password (needs your passphrase
              the first time; gpg-agent caches it for a while after)
```

**EXPLANATION FOR SETTING UP A VAULT FROM SCRATCH AND ADDING YOUR GITHUB PERSONAL ACCESS TOKEN (PAT) TO IT AS AN EXAMPLE**

Using a PAT specifically, not a password, because GitHub doesn't accept account
passwords for git/API operations at all anymore — a PAT is what actually goes in that
prompt. Generate one at github.com → Settings → Developer settings → Personal access
tokens.
```
Thing            Where it comes from              What it's actually for
─────────────────────────────────────────────────────────────────────────
Name / Email     You type it when you run         The vault never reads this
(= "User ID")    `gpg --full-generate-key`         — but YOU will. It's the
                 to create your key                only human-readable label
                                                    you'll see when running
                                                    `gpg --list-keys` later.
                                                    Pick something you'll
                                                    recognize (e.g. name:
                                                    "pass-vault"), not
                                                    garbage — you're the one
                                                    who has to remember it,
                                                    not the software.

Passphrase       You type it when you run         This passphrase allows
                 `gpg --full-generate-key`,        you to get into your
                 same command as above             vault.

Key ID           GPG generates this on its        An ID number you give to
(long hex        own, shown to you after           `pass init` one time, to
string)          you run `gpg --list-secret-       tell your (still-empty)
                 keys`                             vault which key to use.

Public key       Generated automatically           Locks up new passwords
                 alongside the key, same           you save — used the
                 command as above                  moment you run
                                                    `pass insert github`.

Private key      Generated automatically           Unlocks passwords so you
                 alongside the key, same           can read them — used the
                 command as above                  moment you run
                                                    `pass show github` (once
                                                    the passphrase has
                                                    unlocked the key itself).

─────────────────────────────────────────────────────────────────────────
Your Actual      You type it when you run          THIS is your actual
GitHub           `pass insert github` — pass       GitHub PAT — the real
Personal         then asks you for it on its       credential git sends to
Access Token     OWN separate line, AFTER you      GitHub over HTTPS. Lives
(PAT)            run that command                  INSIDE the vault,
                                                    encrypted. Retrieved
                                                    with `pass show github`.
                                                    GitHub sees THIS, never
                                                    the passphrase. NOT the
                                                    same as, and unrelated
                                                    to, the passphrase
                                                    above. NOT your GitHub
                                                    account password either
                                                    — GitHub no longer
                                                    accepts that for git/API
                                                    use at all.
```

Once set up, a tool call looks like:
```json
{"expect": "Password for", "send_secret": "github"}
```
Note: git's prompt says "Password for ..." although the PAT belongs there; the
`expect` regex has to match what git actually prints.

The model only ever sees the word `"github"`, never your real PAT.

```
BELOW IS HOW YOU BLOW THE WHOLE VAULT AWAY IF YOU WANT START OVER
═════════════════════════════════════════════════════════════════
gpgconf --kill gpg-agent
rm -rf ~/.password-store
```

Example run
═══════════

```
$ python main.py 
[mcp] disabled in config.toml — no workers
> please run sudo whoami
Response:
please select the cred name I need to use:
super_secret_admin_password
> super_secret_admin_password
Response:
`sudo whoami` returned **`root`** — the `super_secret_admin_password` credential authenticated successfully.
```

The transcript the model receives shows the password as `***`.

</details>

## Configuration

Non-secret settings live in `config.toml`. Secrets stay in the environment; the app
does **not** read a `.env` file. The file ships with `[mcp].enabled = false`, so a
fresh clone runs on the 26 local tools alone.

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

A worker that is unreachable prints a warning and is skipped, so one being down
doesn't stop the app. `config.toml` is committed and never holds a token, only the
*name* of the variable that holds one. Generate a token with
`python -c "import secrets; print(secrets.token_urlsafe(32))"`.

See [Adding workers](#adding-workers) for what each field does and how to bring a
worker online.

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Read from the shell. The app does not load a `.env`. |
| `CLAUDE_MEMORY_DIR` | Where `memory` stores `/memories`. Defaults to `./memories` **relative to the working directory** — set it (see [Setup](#setup-linux) step 4). |
| `CLAUDE_SHOW_USAGE=1` | Per-request token and prompt-cache counters. |
| `CLAUDE_KERNEL_ENCRYPTION` | `auto` (default) tries CurveZMQ-encrypted TCP, then IPC, then plaintext TCP, printing why each tier fell through; `required` fails the tool rather than running unencrypted; `off` skips encryption. Covers *this* machine's kernel only; a worker's kernel reads the variable from the worker's own environment. |
| `CLAUDE_DISPLAY_SIZE` | `WxH`, the logical display size `computer` declares to the model. Default `1280x800`. |
| `CLAUDE_COMPUTER_FORCE=1` | Use X11/XTEST for `computer` on a Wayland session (XWayland-only setups, nested X servers). |
| `CLAUDE_COMPUTER_MONITOR` | Monitor index, counted left to right, for `computer` on Wayland. Default: the leftmost shared one. |
| `RESEARCHMESH_DOWNLOAD_DIR` | Where browser downloads land. Default `~/Downloads`. |
| `PASSWORD_STORE_DIR` | The `pass` store whose entry names `interactive_run` and `browser_fill` offer. Default `~/.password-store`. |
| *(per worker)* | Each `token_env` names the variable holding that worker's bearer token. No `token_env` means unauthenticated. |
| *(embeddings server)* | Whatever `[embeddings].api_key_env` names, if your server needs auth. |
| *(vision server)* | Whatever `[vision].api_key_env` names, if your server needs auth. |

<a id="adding-workers"></a>

## Adding workers

Any MCP server works; ResearchMesh is what the router was built and tested
against. Each worker's tools are prefixed with its `name`, so identical machines
never collide.

On each worker machine, run ResearchMesh as a server:

```bash
export RESEARCHMESH_MCP_TOKEN=...
python mcp_server.py --transport streamable-http --host 0.0.0.0 --port 8100
```

Then add it to `config.toml` here (full example under
[Configuration](#configuration)). Four fields deserve a second look.

**`name` becomes the tool prefix** (`gpu-box__delegate`). Keep it short. Letters,
digits, `_` and `-` pass through unchanged, and anything else is replaced with
`_`, so `gpu box` and `gpu.box` would collide (`gpu-box` needs no substitution).
A collision gets a numeric suffix instead of silently shadowing one worker's tool
with the other's.

**`description` is strongly recommended.** ResearchMesh hardcodes one description
constant, so every worker describes itself identically unless you add your own.
Write what is true of *that* machine: its OS and session type, what is installed
or attached, what data is on it, what it must not be used for. It is prepended to
that worker's tools as `[worker: name] ...` and makes routing much more reliable.

**`timeout_seconds` matters more than it looks.** The MCP SDK defaults to 300s. A
worker driving a GUI runs longer than that, and when the timeout fires the work
is already done on the far side and lost. The router default is 900s; raise it
per worker for long compute. The *connect* timeout stays at 15s, so a machine
that is switched off fails in seconds instead of hanging the turn.

**`url` can be `https://`.** The router adds no certificate logic; it uses the
HTTP client's normal trust configuration (the OS trust store). A company CA or
private certificate works only if that CA is already trusted on the client
machine, or if `SSL_CERT_FILE=/path/ca.pem` or `SSL_CERT_DIR=/path/to/certs` is
set for that process. The certificate itself belongs on the *worker*
(`mcp_server.py --ssl-certfile/--ssl-keyfile`). Over plain `http://` the bearer
token and every task and result cross the network in the clear: acceptable on a
trusted LAN, not on a corporate one.

**How work is distributed.** Worker calls are grouped by machine. Groups run
concurrently and calls within a group run in order, because a ResearchMesh worker
has one mouse, one browser page and one kernel, and serialises `delegate` behind
a lock. So `max_parallel` is really how many machines at once. Local tools run in
order. A turn calling three workers takes as long as the slowest, not the sum.

<details>
<summary><b>Project layout and extending</b></summary>

```
main.py           entrypoint — loads config, connects the fleet, runs the REPL
mcp_client.py     MCP client (stdio / SSE / Streamable HTTP)
config.toml       the fleet, and router behaviour
smoke_test.py     the offline gate
e2e_test.py       live check against real workers (costs tokens, not a gate)
e2e_worker.py     the stand-in worker e2e_test.py launches
test_model_compat_live.py  live check of the per-model tool handler (costs tokens, not a gate)
test_*.py         behavioural tests for individual tools (no API; see CLAUDE.md, Commands)
core/
  chat.py         the agentic loop, routing prompt, /dagent
  claude.py       Anthropic SDK wrapper
  cli.py          prompt_toolkit REPL
  tools.py        namespacing, worker identity, fan-out  ← the reason this exists
  local_tools.py  registry — the one place a local tool is wired in
  browser.py  browser_session.py  computer.py  wayland_input.py  kernel.py  bash_session.py  memory.py  data.py
  documents.py  processes.py  config_edit.py  files.py  output.py
  claude_learned_schemas.py  text_embeddings.py  vision.py  speak.py
  listen.py  process_reaper.py  desktop_window.py  screen_find.py  dbus_loop.py
```

Adding a **worker** is a config edit, no code. Adding a **local tool** is one
module exposing `TOOLS`/`handles()`/`execute()`, plus a line in `local_tools.py`.

</details>

## Recommended local tools — install on every machine (saves tokens)

The app starts without these, but Claude works faster and cheaper with them: it
reaches for a purpose-built local binary through `bash` instead of spending tokens
re-implementing the job in `python`, or reading whole files through the editor to
search them. Install them on the router and on each worker. Everything below is
`apt`, `snap` or `flatpak` (Rust uses the official `rustup` installer), and the
commands are Debian/Ubuntu-specific; on another distro the tool names are the
same, so use your own package manager. The `apt` and `flatpak` lines include `-y`
because Claude may run them itself through `bash`, which has no terminal to prompt
on; drop it if you want to review each one by hand.

**This is the router's own machine only.** Each worker is a separate ResearchMesh
install with its own `bash`, filesystem and set of tools; installing something
here does not make it available on `gpu-box` or `scraper`. Repeat the installs on
each worker.

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
# tar, gzip, zip, unzip and xz-utils are already on a standard Ubuntu install, which
# covers nearly every format. The gaps:
sudo apt install -y unrar            # RAR extraction (RAR is proprietary; nothing built in)
# 7-Zip's .7z format; add it only if you receive .7z files:
sudo apt install -y 7zip             # provides `7z`; current Ubuntu has `7zip`, not `p7zip-full`
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
# gcc/g++/make (build-essential) come from Setup step 1. clang is an alternative compiler:
sudo apt install -y clang            # self-contained C/C++ compiler, alternative to gcc
sudo apt install -y cmake            # build system generator
sudo apt install -y ninja-build      # fast build backend, pairs with cmake
# Rust: use the official rustup installer; apt's rustc/cargo lag well behind upstream
# and cannot be updated separately from the system:
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh

# --- System diagnostics ---------------------------------------------------------------
# strace and lsof ship with a standard Ubuntu install (`ubuntu-standard`); nothing to add.
# `strace <cmd>` traces syscalls (first move for "why is this hanging"); `lsof` shows
# what has a file or port open.
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
# DaVinci Resolve (another free NLE) has no apt/snap/flatpak package; Blackmagic
# distributes it by manual download after a free signup.

# --- Documents & writing -----------------------------------------------------------------
sudo apt install -y poppler-utils   # pdftotext/pdftoppm/pdfinfo/pdfimages — pull just the pages you
                                  # need out of a PDF as text, without going through LibreOffice
sudo apt install -y calibre         # ebook-convert (CLI) — epub/mobi/azw3/etc., more formats than
                                  # document_convert reaches
sudo apt install -y hunspell        # command-line spell-checking
```

## Origin

The CLI shell, Anthropic wrapper and MCP client began as copies from
[ResearchMesh](https://github.com/nodormu/ResearchMesh) (same author, MIT), and
the tool modules are copies kept identical in code. Docstrings and comments here
are shorter, so compare the parsed code with docstrings stripped, not the bytes.
The files in `core/` that differ in code are `browser.py`, `chat.py`, `cli.py`,
`computer.py`, `local_tools.py` and `tools.py`; `browser_session.py`,
`dbus_loop.py`, `desktop_window.py`, `screen_find.py` and `wayland_input.py`
exist only here, and `midi1.py` exists only in ResearchMesh. Anything else that
differs in code is drift. A fix to a tool in either repo should be a copy of the
code.

It exists because a plain MCP bridge passes tool names through verbatim, so three
ResearchMesh workers all advertising `delegate` get rejected outright
(`400 ... Tool names must be unique`). [core/tools.py](core/tools.py) is the fix.
**If Claude Code is your front end you don't need any of this**: it already
namespaces MCP tools as `mcp__<server>__<tool>`.

## Deliberately not built

- **No loop protection.** Nothing stops a worker's config pointing back here.

## License

[MIT](LICENSE) — use it, fork it, ship it. No warranty; see the file for the full text.
