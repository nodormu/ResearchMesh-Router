"""SQL over local files — DuckDB, no import step.

`SELECT * FROM 'data.csv'` works directly, as does Parquet, JSON, and a
glob of any of them. The connection is in-memory and reused for the whole
session, so views and temp tables created in one call are still there in the
next; ATTACH a .duckdb file if something needs to outlive the session.

Requires:  pip install duckdb
"""

import asyncio
import json

TOOLS = [
    {
        "name": "sql_query",
        "description": (
            "Run a SQL query with DuckDB. Reads CSV, Parquet, JSON, and Excel "
            "files in place with no import step — SELECT * FROM 'sales.csv' or "
            "FROM 'logs/*.parquet' — and file globs work as tables. Views and "
            "tables you create persist for the rest of the session. Prefer this "
            "over loading a data file into Python when the question is a query. "
            "Returns JSON with columns and rows."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "SQL to execute.",
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        "Maximum rows to return (default 100). The query still "
                        "runs in full; this only caps what comes back."
                    ),
                },
            },
            "required": ["query"],
        },
    }
]

_TOOL_NAMES = {t["name"] for t in TOOLS}
_DEFAULT_LIMIT = 100

_connection = None


def handles(name: str) -> bool:
    return name in _TOOL_NAMES


async def execute(name: str, tool_input: dict) -> str:
    if name != "sql_query":
        return json.dumps({"error": f"unknown data tool {name!r}"})
    return await asyncio.to_thread(_run, tool_input)


def _cell(value):
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)  # dates, decimals, blobs, nested types


def _run(tool_input: dict) -> str:
    global _connection

    query = tool_input.get("query", "")
    if not query.strip():
        return json.dumps({"error": "no query provided"})
    limit = int(tool_input.get("limit") or _DEFAULT_LIMIT)

    if _connection is None:
        try:
            import duckdb
        except ImportError:
            return json.dumps(
                {
                    "error": "duckdb is not installed — `pip install duckdb` to "
                    "enable the sql_query tool"
                }
            )
        try:
            _connection = duckdb.connect(":memory:")
        except Exception as e:
            return json.dumps({"error": f"could not open DuckDB: {e}"})

    try:
        cursor = _connection.execute(query)
    except Exception as e:
        return json.dumps({"error": f"{type(e).__name__}: {e}"})

    if cursor.description is None:  # DDL / DML, nothing to fetch
        return json.dumps({"columns": [], "rows": [], "row_count": 0, "statement_ok": True})

    columns = [d[0] for d in cursor.description]
    rows = cursor.fetchmany(limit + 1)
    truncated = len(rows) > limit

    return json.dumps(
        {
            "columns": columns,
            "rows": [[_cell(v) for v in row] for row in rows[:limit]],
            "row_count": min(len(rows), limit),
            "truncated": truncated,
        }
    )


def close():
    global _connection
    if _connection is not None:
        import duckdb  # already loaded — a live _connection implies a prior success

        try:
            _connection.close()
        except duckdb.Error as e:
            print(f"[data] duckdb close failed (ignored): {e}")
        _connection = None
