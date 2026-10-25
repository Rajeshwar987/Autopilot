"""Guardrails for agent-supplied SQL: statement allow-lists, result caps, timeouts, formatting.
These are the second wall. The first is that agents only ever touch a sandbox file opened with
`enable_external_access = false` (no file reads, no ATTACH, no extensions), and prod is written by
exactly one function that requires a recorded human approval."""
from __future__ import annotations

import re
import threading
from typing import Any

import duckdb

READONLY_FIRST = {"SELECT", "WITH", "DESCRIBE", "SHOW", "SUMMARIZE", "EXPLAIN", "FROM"}
BACKFILL_FIRST = {"UPDATE", "DELETE", "INSERT", "ALTER"}
DENY_ANYWHERE = re.compile(
    r"\b(DROP|TRUNCATE|ATTACH|DETACH|COPY|EXPORT|IMPORT|INSTALL|LOAD|PRAGMA|CALL|CHECKPOINT|"
    r"read_csv\w*|read_parquet|read_json\w*|read_text|read_blob|glob|getenv|sniff_csv|httpfs)\b",
    re.IGNORECASE,
)
RESULT_CHAR_CAP = 4000


class SQLGuardError(ValueError):
    pass


def strip_comments(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    sql = re.sub(r"--[^\n]*", " ", sql)
    return sql.strip()


def _without_literals(sql: str) -> str:
    return re.sub(r"'(?:[^']|'')*'", "''", sql)


def single_statement(sql: str) -> str:
    """Return the statement without a trailing semicolon; reject multiple statements."""
    s = strip_comments(sql).rstrip().rstrip(";").strip()
    if ";" in _without_literals(s):
        raise SQLGuardError("only one SQL statement is allowed per call")
    if not s:
        raise SQLGuardError("empty SQL")
    return s


def first_keyword(sql: str) -> str:
    m = re.match(r"\s*([A-Za-z]+)", sql)
    return m.group(1).upper() if m else ""


def _deny_scan(sql: str) -> None:
    m = DENY_ANYWHERE.search(_without_literals(sql))
    if m:
        raise SQLGuardError(f"'{m.group(1)}' is not allowed in agent SQL")


def validate_readonly(sql: str) -> str:
    s = single_statement(sql)
    if first_keyword(s) not in READONLY_FIRST:
        raise SQLGuardError(f"read-only tool: statement must start with one of {sorted(READONLY_FIRST)}")
    _deny_scan(s)
    return s


def validate_where_fragment(where: str) -> str:
    w = strip_comments(where or "").strip()
    if not w:
        return "true"
    if ";" in _without_literals(w):
        raise SQLGuardError("where fragment must not contain ';'")
    _deny_scan(w)
    return w


def validate_model_sql(sql: str, model_name: str) -> str:
    s = single_statement(sql)
    pat = rf"^\s*CREATE\s+OR\s+REPLACE\s+(TABLE|VIEW)\s+(\"?){re.escape(model_name)}\2\s+AS\b"
    if not re.match(pat, s, flags=re.IGNORECASE):
        raise SQLGuardError(f"model SQL must start with 'CREATE OR REPLACE TABLE {model_name} AS'")
    _deny_scan(s)
    return s


def validate_backfill(sql: str) -> list[str]:
    """Backfill may contain several statements separated by ';', each UPDATE/DELETE/INSERT/ALTER."""
    stmts = [p.strip() for p in _split_statements(strip_comments(sql)) if p.strip()]
    for st in stmts:
        kw = first_keyword(st)
        if kw not in BACKFILL_FIRST:
            raise SQLGuardError(f"backfill statements must start with one of {sorted(BACKFILL_FIRST)} (got {kw})")
        if kw in {"UPDATE", "DELETE"} and not re.search(r"\bWHERE\b", _without_literals(st), re.IGNORECASE):
            raise SQLGuardError("UPDATE/DELETE in backfill must have a WHERE clause")
        if kw == "ALTER" and not re.search(r"\bADD\s+COLUMN\b", st, re.IGNORECASE):
            raise SQLGuardError("only ALTER TABLE ... ADD COLUMN is allowed")
        _deny_scan(st)
    return stmts


def _split_statements(sql: str) -> list[str]:
    out, buf, in_str = [], [], False
    for ch in sql:
        if ch == "'":
            in_str = not in_str
        if ch == ";" and not in_str:
            out.append("".join(buf)); buf = []
        else:
            buf.append(ch)
    out.append("".join(buf))
    return out


def execute_with_timeout(conn: duckdb.DuckDBPyConnection, sql: str, timeout_s: float, params: list | None = None):
    timer = threading.Timer(timeout_s, conn.interrupt)
    timer.start()
    try:
        return conn.execute(sql, params or [])
    except duckdb.InterruptException as e:
        raise SQLGuardError(f"query exceeded {timeout_s:.0f}s and was cancelled") from e
    finally:
        timer.cancel()


def format_rows(cur, max_rows: int = 50) -> str:
    cols = [d[0] for d in cur.description] if cur.description else []
    rows = cur.fetchmany(max_rows + 1) if cur.description else []
    if not cols:
        return "(no result set)"
    more = len(rows) > max_rows
    rows = rows[:max_rows]
    def cell(v: Any) -> str:
        s = "NULL" if v is None else str(v)
        return s if len(s) <= 60 else s[:57] + "..."
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    lines += ["| " + " | ".join(cell(v) for v in r) + " |" for r in rows]
    if more:
        lines.append(f"... more rows (showing first {max_rows})")
    if not rows:
        lines.append("(0 rows)")
    return "\n".join(lines)


def run_readonly(conn: duckdb.DuckDBPyConnection, sql: str, limit: int = 50, timeout_s: float = 10.0) -> str:
    s = validate_readonly(sql)
    if first_keyword(s) in {"SELECT", "WITH", "FROM"}:
        s = f"SELECT * FROM ({s}) AS _q LIMIT {int(limit) + 1}"  # +1 so the cap is visible to the agent
    cur = execute_with_timeout(conn, s, timeout_s)
    return cap(format_rows(cur, limit))


def cap(text: str, limit: int = RESULT_CHAR_CAP) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... (truncated, {len(text) - limit} more chars; ask a narrower question)"
