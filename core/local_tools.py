"""Registry of the client-executed tools.

Every module listed in MODULES exposes the same three names — `TOOLS` (Anthropic
tool schemas), `handles(name)`, and `await execute(name, input)` — so adding a
tool means writing one module and adding it here, rather than editing the chat
loop's declaration list and its routing chain separately.

Optional third-party packages are imported inside each module's `execute`, so a
tool whose dependency is missing declares itself normally and returns an install
hint if the model reaches for it.
"""

import inspect

from core import (
    browser,
    computer,
    config_edit,
    data,
    documents,
    files,
    kernel,
    memory,
    processes,
)
from core import claude_learned_schemas as learned

MODULES = [
    learned,     # bash, text editor, web_search, web_fetch
    memory,      # cross-session memory (learned schema)
    computer,    # screen/mouse/keyboard control (learned schema, beta-gated)
    browser,     # Playwright DOM surfing
    documents,   # LibreOffice / pandoc conversion
    kernel,      # stateful IPython
    processes,   # pexpect interactive commands
    config_edit,  # comment-preserving YAML/TOML/JSON edits
    data,        # DuckDB
    files,       # trash
]

TOOLS = [tool for module in MODULES for tool in module.TOOLS]

_DUPLICATES = {
    name
    for name in (t["name"] for t in TOOLS)
    if [t["name"] for t in TOOLS].count(name) > 1
}
if _DUPLICATES:
    raise ValueError(f"duplicate local tool names: {sorted(_DUPLICATES)}")


def handles(name: str) -> bool:
    return any(module.handles(name) for module in MODULES)


async def execute(name: str, tool_input: dict) -> str | None:
    """Run a local tool, or return None if no local module owns that name."""
    for module in MODULES:
        if module.handles(name):
            return await module.execute(name, tool_input)
    return None


async def shutdown():
    """Release everything a local tool may have started. Safe if unused.

    Each step is isolated: these run as an AsyncExitStack callback on the way
    out, so an exception escaping one of them would both skip every later step
    (leaking whatever it owns) and turn an ordinary Ctrl-C into a traceback.
    One tool failing to clean up must not stop the others from trying.
    """
    for label, close in (
        ("browser", browser.shutdown),
        ("kernel", kernel.shutdown),
        ("sql_query", data.close),
    ):
        try:
            result = close()
            if inspect.isawaitable(result):
                await result
        except Exception as e:
            print(f"[shutdown] {label} cleanup failed (ignored): {e}")
