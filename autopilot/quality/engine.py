"""YAML-driven check engine. Every check compiles to one SQL statement so the diagnosis agent can see
exactly what failed. ~120 lines instead of Great Expectations / soda-core."""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import duckdb
import yaml

from autopilot import config


@dataclass
class CheckResult:
    check_name: str
    table_name: str
    status: str  # passed | failed | error
    failed_rows: int | None = None
    total_rows: int | None = None
    sql_text: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_checks(path: Path | None = None) -> list[dict[str, Any]]:
    with open(path or config.CHECKS_FILE, encoding="utf-8") as f:
        return yaml.safe_load(f) or []


def _scope(run_id: str | None) -> str:
    return f"run_id = '{run_id}'" if run_id else "true"


def _sql_literal(v: Any) -> str:
    return f"'{v}'" if isinstance(v, str) else str(v)


def compile_check(chk: dict[str, Any], run_id: str | None) -> str:
    """Return SQL yielding exactly one row: (failed_rows BIGINT, total_rows BIGINT, extra VARCHAR)."""
    t, where, typ = chk["table"], _scope(run_id), chk["type"]
    col = chk.get("column")
    if typ == "not_null":
        return (f"SELECT count(*) FILTER (WHERE {col} IS NULL) AS failed_rows, count(*) AS total_rows, "
                f"NULL AS extra FROM {t} WHERE {where}")
    if typ == "range":
        return (f"SELECT count(*) FILTER (WHERE {col} IS NOT NULL AND ({col} < {chk['min']} OR {col} > {chk['max']})) "
                f"AS failed_rows, count(*) AS total_rows, "
                f"concat('min=', min({col}), ' median=', median({col}), ' max=', max({col})) AS extra "
                f"FROM {t} WHERE {where}")
    if typ == "unique":
        cols = ", ".join(chk["columns"])
        return (f"SELECT coalesce(sum(n - 1), 0)::BIGINT AS failed_rows, coalesce(sum(n), 0)::BIGINT AS total_rows, "
                f"concat('duplicated_keys=', count(*) FILTER (WHERE n > 1)) AS extra "
                f"FROM (SELECT {cols}, count(*) AS n FROM {t} WHERE {where} GROUP BY {cols})")
    if typ == "row_count_min":
        return f"SELECT 0 AS failed_rows, count(*) AS total_rows, concat('min_expected=', {chk['min']}) AS extra FROM {t} WHERE {where}"
    if typ == "freshness":
        return (f"SELECT 0 AS failed_rows, count(*) AS total_rows, "
                f"concat('snapshot_time=', max(snapshot_time), ' now=', epoch(now())::BIGINT, "
                f"' age_s=', epoch(now())::BIGINT - max(snapshot_time)) AS extra FROM {t} WHERE {where}")
    if typ == "accepted_values":
        vals = ", ".join(_sql_literal(v) for v in chk["values"])
        return (f"SELECT count(*) FILTER (WHERE {col} IS NOT NULL AND {col} NOT IN ({vals})) AS failed_rows, "
                f"count(*) AS total_rows, concat('distinct=', count(DISTINCT {col})) AS extra FROM {t} WHERE {where}")
    if typ == "expression":
        return (f"SELECT count(*) FILTER (WHERE NOT coalesce(({chk['expression']}), true)) AS failed_rows, "
                f"count(*) AS total_rows, NULL AS extra FROM {t} WHERE {where}")
    raise ValueError(f"unknown check type: {typ}")


def evaluate(chk: dict[str, Any], failed: int, total: int, extra: str | None,
             conn: duckdb.DuckDBPyConnection, run_id: str | None, context: dict[str, Any]) -> tuple[str, dict]:
    """Turn the counts into passed/failed plus details, applying thresholds."""
    typ = chk["type"]
    details: dict[str, Any] = {"severity": chk.get("severity", "medium")}
    if extra:
        details["stats"] = extra
    pct = (100.0 * failed / total) if total else 0.0
    if typ == "row_count_min":
        ok = total >= int(chk["min"])
        details["min_expected"] = chk["min"]
    elif typ == "freshness":
        snap = conn.execute(f"SELECT max(snapshot_time) FROM {chk['table']} WHERE {_scope(run_id)}").fetchone()[0]
        now_epoch = int(context.get("now_epoch") or time.time())
        prev = context.get("previous_snapshot_time")
        age = None if snap is None else now_epoch - int(snap)
        details.update({"snapshot_time": snap, "age_seconds": age, "previous_snapshot_time": prev})
        too_old = snap is None or abs(age) > int(chk.get("max_age_seconds", 900))
        not_advancing = prev is not None and snap is not None and int(snap) <= int(prev)
        if too_old:
            details["reason"] = "snapshot_time outside max_age window (or in the future)"
        if not_advancing:
            details["reason"] = "snapshot_time did not advance since previous run"
        ok = not (too_old or not_advancing)
        if not ok:
            failed = total
    elif typ == "not_null":
        details["null_pct"] = round(pct, 2)
        ok = pct <= float(chk.get("max_null_pct", 0))
    else:
        details["failed_pct"] = round(pct, 2)
        ok = (pct <= float(chk["max_failed_pct"])) if "max_failed_pct" in chk else failed == 0
    return ("passed" if ok else "failed"), details


def run_checks(conn: duckdb.DuckDBPyConnection, run_id: str | None = None,
               context: dict[str, Any] | None = None, checks: list[dict[str, Any]] | None = None) -> list[CheckResult]:
    """Run all checks against `conn`, scoped to `run_id` (None = whole table, used in the sandbox to
    make sure a fix does not break historical rows)."""
    context = context or {}
    results: list[CheckResult] = []
    for chk in checks or load_checks():
        sql = compile_check(chk, run_id)
        try:
            failed, total, extra = conn.execute(sql).fetchone()
            failed, total = int(failed or 0), int(total or 0)
            status, details = evaluate(chk, failed, total, extra, conn, run_id, context)
            if chk.get("description"):
                details["description"] = chk["description"]
            results.append(CheckResult(chk["name"], chk["table"], status, failed, total, sql, details))
        except duckdb.Error as e:
            results.append(CheckResult(chk["name"], chk["table"], "error", None, None, sql, {"error": str(e)}))
    return results


def summarize(results: list[CheckResult]) -> dict[str, Any]:
    failed = [r for r in results if r.status != "passed"]
    return {"total": len(results), "passed": len(results) - len(failed),
            "failed": [r.check_name for r in failed],
            "rows": {r.check_name: {"status": r.status, "failed_rows": r.failed_rows, "total_rows": r.total_rows}
                     for r in results}}
