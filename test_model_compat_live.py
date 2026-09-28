"""Live check of the per-model tool-compatibility handler.

    python test_model_compat_live.py

Needs ANTHROPIC_API_KEY, spends tokens (~9 requests), not in CI. Sends the full local tool list to
every model in config.toml's claude_models through the real Claude.chat(), twice (config order, then
reversed), and asserts:
  * every model accepts the tools and replies, in at most one retry
  * a model that needed the retry lost exactly the types the API named
  * pass 2 makes one request per model: no rediscovery, no leakage between models
This is the only check that catches Anthropic rewording the "does not support tool types" error;
smoke_test.py uses a fake. If no model rejects any tool, the wording check was not exercised (printed).

Exit: 0 pass, 1 fail, 2 not run (no API key).
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name}{' — ' + detail if detail else ''}")
        FAILURES.append(name)


ACCEPTED_STOP_REASONS = {"end_turn", "tool_use"}


def accepted(reply) -> bool:
    return reply is not None and reply.stop_reason in ACCEPTED_STOP_REASONS


def why(reply, err) -> str:
    return repr(err) if err is not None else f"stop_reason={getattr(reply, 'stop_reason', None)!r}"


class CountingClient:
    """Wraps the real client; records every request's model and tool types,
    then forwards it unchanged to the real API."""

    def __init__(self, real) -> None:
        self._real = real
        self.calls: list[tuple[str, list[str]]] = []
        self.beta = self
        self.messages = self

    def create(self, **params):
        tools = params.get("tools") or []
        self.calls.append(
            (params["model"], [t.get("type") or t.get("name") for t in tools])
        )
        return self._real.beta.messages.create(**params)


def main() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set — this live test did NOT run.")
        return 2
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))

    from core import local_tools
    from core.claude import Claude, load_claude_models

    models = load_claude_models()
    tools = local_tools.TOOLS
    declared = [t.get("type") or t.get("name") for t in tools]
    print(f"{len(models)} models x {len(declared)} declared tools: {models}")

    cl = Claude(models[0])
    counter = CountingClient(cl.client)
    cl.client = counter  # type: ignore[assignment]

    def turn(model: str):
        cl.model = model
        before = len(counter.calls)
        try:
            reply = cl.chat(
                [{"role": "user", "content": "Reply with the single word: ok"}],
                tools=tools,
            )
            err = None
        except Exception as e:
            reply, err = None, e
        return reply, counter.calls[before:], err

    print("\npass 1: config order")
    rejected: dict[str, set[str]] = {}
    for m in models:
        reply, calls, err = turn(m)
        check(f"{m}: request accepted, reply received", accepted(reply), why(reply, err))
        check(f"{m}: at most one retry", 1 <= len(calls) <= 2, f"{len(calls)} requests")
        if len(calls) == 2:
            bad = cl._unsupported_by_model.get(m, set())
            rejected[m] = set(bad)
            check(f"{m}: the retry recorded what the API rejected", bool(bad), str(bad))
            kept = [t for t in declared if t not in bad]
            check(
                f"{m}: the retry dropped exactly the rejected types and kept the rest",
                calls[1][1] == kept,
                f"sent {calls[1][1]} expected {kept}",
            )
        else:
            check(f"{m}: nothing recorded when nothing was rejected", m not in cl._unsupported_by_model)

    print("\npass 2: reversed order, same Claude object")
    for m in reversed(models):
        reply, calls, err = turn(m)
        check(f"{m}: request accepted again", accepted(reply), why(reply, err))
        check(f"{m}: exactly one request, no rediscovery", len(calls) == 1, f"{len(calls)} requests")
        expected = [t for t in declared if t not in rejected.get(m, set())]
        check(
            f"{m}: sent its own tool set, unaffected by other models",
            bool(calls) and calls[0][1] == expected,
            f"sent {calls[0][1] if calls else None} expected {expected}",
        )

    print()
    if rejected:
        print(f"models that rejected tools this run: { {m: sorted(t) for m, t in rejected.items()} }")
    else:
        print(
            "NOTE: no configured model rejected any tool, so the handler's parsing of "
            "Anthropic's error wording was NOT exercised against the live API this run."
        )
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): {', '.join(FAILURES)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
