"""Comment-preserving config edits — ruamel.yaml, tomlkit, stdlib json.

A `sed` line or a yaml.safe_load -> yaml.dump round-trip silently strips every
comment, blank line, and quoting choice in the file. The configs in this project
are mostly comment, so that is real data loss with no error message. These three
parsers keep the document intact and change only the key asked for.

Requires:  pip install ruamel.yaml tomlkit        (json needs nothing)
           pip install jsonpath-ng               (only for '$.…' queries)
"""

import asyncio
import io
import json
import os
import re
from pathlib import Path

TOOLS = [
    {
        "name": "config_edit",
        "description": (
            "Read or change one key in a YAML, TOML, or JSON config file WITHOUT "
            "destroying the rest of the document — comments, key order, blank "
            "lines, and quoting style all survive. Always prefer this over editing "
            "a config with the file editor or sed, both of which routinely mangle "
            "comments. Address keys with a dotted path ('claude.model', "
            "'servers[0].url'); a path starting with '$' is treated as a JSONPath "
            "query (read-only)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the .yaml/.yml, .toml, or .json file.",
                },
                "operation": {
                    "type": "string",
                    "enum": ["get", "set", "delete"],
                    "description": "What to do with the key (default 'get').",
                },
                "key_path": {
                    "type": "string",
                    "description": (
                        "Dotted path to the key, e.g. 'claude.model' or "
                        "'tools[2].name'. Omit to read the whole document."
                    ),
                },
                "value": {
                    "description": (
                        "New value for 'set'. Any JSON type — string, number, "
                        "boolean, list, or object."
                    ),
                },
            },
            "required": ["path"],
        },
    }
]

_TOOL_NAMES = {t["name"] for t in TOOLS}
_INDEX = re.compile(r"\[(\d+)\]")

_YAML_SUFFIXES = {".yaml", ".yml"}


def handles(name: str) -> bool:
    return name in _TOOL_NAMES


async def execute(name: str, tool_input: dict) -> str:
    if name != "config_edit":
        return json.dumps({"error": f"unknown config tool {name!r}"})
    return await asyncio.to_thread(_run, tool_input)


def _err(message: str) -> str:
    return json.dumps({"error": message})


def _tokens(key_path: str) -> list:
    """'a.b[0].c' -> ['a', 'b', 0, 'c']"""
    out: list = []
    for part in key_path.split("."):
        if not part:
            continue
        head = _INDEX.split(part)
        # re.split on a capturing group interleaves text and indices.
        for i, piece in enumerate(head):
            if piece == "":
                continue
            out.append(int(piece) if i % 2 else piece)
    return out


def _walk(doc, tokens: list):
    """Resolve every token, raising KeyError/IndexError/TypeError on a miss."""
    node = doc
    for token in tokens:
        node = node[token]
    return node


def _plain(value):
    """Strip parser-specific wrapper types so json.dumps can serialise."""
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


# --- format handlers ----------------------------------------------------


def _load(path: Path):
    """Returns (document, dumper, format_name) or raises RuntimeError."""
    suffix = path.suffix.lower()
    text = path.read_text(encoding="utf-8")

    if suffix in _YAML_SUFFIXES:
        try:
            from ruamel.yaml import YAML
        except ImportError:
            raise RuntimeError(
                "ruamel.yaml is not installed — `pip install ruamel.yaml` to edit "
                "YAML without stripping its comments"
            )
        yaml = YAML()
        yaml.preserve_quotes = True
        yaml.width = 4096  # don't re-wrap long lines on the way out

        def dump(doc) -> str:
            stream = io.StringIO()
            yaml.dump(doc, stream)
            return stream.getvalue()

        return yaml.load(text), dump, "yaml"

    if suffix == ".toml":
        try:
            import tomlkit
        except ImportError:
            raise RuntimeError(
                "tomlkit is not installed — `pip install tomlkit` to edit TOML "
                "without stripping its comments"
            )
        return tomlkit.parse(text), tomlkit.dumps, "toml"

    if suffix == ".json":
        return (
            json.loads(text),
            lambda doc: json.dumps(doc, indent=2) + "\n",
            "json",
        )

    raise RuntimeError(
        f"unsupported config format {suffix!r} — this tool handles .yaml/.yml, "
        ".toml, and .json"
    )


def _write(path: Path, text: str):
    """Write via a temp file in the same directory, then rename over the original."""
    scratch = path.with_name(path.name + ".tmp")
    scratch.write_text(text, encoding="utf-8")
    os.replace(scratch, path)


def _jsonpath(doc, expression: str) -> str:
    try:
        from jsonpath_ng.ext import parse
    except ImportError:
        return _err(
            "jsonpath-ng is not installed — `pip install jsonpath-ng` for '$' "
            "queries, or use a dotted key_path instead"
        )
    try:
        matches = parse(expression).find(doc)
    except Exception as e:
        return _err(f"bad JSONPath {expression!r}: {e}")
    return json.dumps(
        {
            "matches": [
                {"path": str(m.full_path), "value": _plain(m.value)}
                for m in matches
            ],
            "count": len(matches),
        }
    )


# --- tool ---------------------------------------------------------------


def _run(tool_input: dict) -> str:
    path = Path(tool_input.get("path", "")).expanduser()
    operation = tool_input.get("operation") or "get"
    key_path = tool_input.get("key_path") or ""

    if not path.is_file():
        return _err(f"no such file: {path}")
    if operation not in ("get", "set", "delete"):
        return _err(f"unknown operation {operation!r} (get, set, or delete)")

    try:
        doc, dump, fmt = _load(path)
    except RuntimeError as e:
        return _err(str(e))
    except Exception as e:
        return _err(f"could not parse {path}: {e}")

    if key_path.startswith("$"):
        if operation != "get":
            return _err("JSONPath expressions are read-only; use a dotted key_path")
        return _jsonpath(doc, key_path)

    tokens = _tokens(key_path)

    if operation == "get":
        if not tokens:
            return json.dumps({"path": str(path), "format": fmt, "value": _plain(doc)})
        try:
            value = _walk(doc, tokens)
        except (KeyError, IndexError, TypeError) as e:
            return _err(f"{key_path} not found in {path}: {e!r}")
        return json.dumps(
            {"path": str(path), "format": fmt, "key_path": key_path, "value": _plain(value)}
        )

    if not tokens:
        return _err(f"operation {operation!r} needs a key_path")

    try:
        parent = _walk(doc, tokens[:-1])
    except (KeyError, IndexError, TypeError) as e:
        container = ".".join(str(t) for t in tokens[:-1]) or "(root)"
        return _err(f"container {container} not found in {path}: {e!r}")

    leaf = tokens[-1]
    had = True
    try:
        old = _plain(parent[leaf])
    except (KeyError, IndexError, TypeError):
        old, had = None, False

    if operation == "delete":
        if not had:
            return _err(f"{key_path} does not exist in {path}")
        try:
            del parent[leaf]
        except Exception as e:
            return _err(f"could not delete {key_path}: {e}")
    else:
        if "value" not in tool_input:
            return _err("operation 'set' needs a value")
        try:
            parent[leaf] = tool_input["value"]
        except Exception as e:
            return _err(f"could not set {key_path}: {e}")

    try:
        _write(path, dump(doc))
    except Exception as e:
        return _err(f"could not write {path}: {e}")

    return json.dumps(
        {
            "path": str(path),
            "format": fmt,
            "operation": operation,
            "key_path": key_path,
            "old_value": old,
            "new_value": None if operation == "delete" else _plain(tool_input["value"]),
            "created_key": operation == "set" and not had,
        }
    )
