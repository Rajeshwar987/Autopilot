"""Generic per-position profiling of bronze_states.state and a drift diff between two ingest runs.
This is schema-agnostic on purpose: it lets the diagnosis agent spot faults nobody wrote a check for
(a dropped field, an inserted field, a unit change, a type flip)."""
from __future__ import annotations

from typing import Any

import duckdb

from autopilot import config

NULL_RATE_DELTA_PTS = 30.0
MEDIAN_RATIO_LIMIT = 3.0
TYPE_MIN_SHARE = 0.95


def _scope(run_id: str | None, exclude_run_id: str | None = None) -> str:
    if run_id:
        return f"run_id = '{run_id}'"
    if exclude_run_id:
        return f"run_id <> '{exclude_run_id}'"
    return "true"


def infer_type(non_null: int, ok_bigint: int, ok_double: int, ok_bool: int) -> str:
    if non_null == 0:
        return "null"
    if ok_bigint / non_null >= TYPE_MIN_SHARE:
        return "bigint"
    if ok_double / non_null >= TYPE_MIN_SHARE:
        return "double"
    if ok_bool / non_null >= TYPE_MIN_SHARE:
        return "boolean"
    return "varchar"


def profile_positions(conn: duckdb.DuckDBPyConnection, run_id: str | None = None,
                      exclude_run_id: str | None = None, table: str = "bronze_states") -> dict[str, Any]:
    """Per-position stats for the rows in scope. Positions are 1-based, matching DuckDB list indexing."""
    where = _scope(run_id, exclude_run_id)
    row = conn.execute(f"SELECT count(*), min(len(state)), max(len(state)), mode(len(state)) FROM {table} WHERE {where}").fetchone()
    n_rows, min_len, max_len, mode_len = row
    positions: dict[int, dict[str, Any]] = {}
    for i in range(1, int(max_len or 0) + 1):
        e = f"state[{i}]"
        r = conn.execute(f"""
            SELECT count({e}) AS non_null,
                   count(*) FILTER (WHERE TRY_CAST({e} AS BIGINT) IS NOT NULL) AS ok_bigint,
                   count(*) FILTER (WHERE TRY_CAST({e} AS DOUBLE) IS NOT NULL) AS ok_double,
                   count(*) FILTER (WHERE TRY_CAST({e} AS BOOLEAN) IS NOT NULL) AS ok_bool,
                   count(DISTINCT {e}) AS distinct_values,
                   min(TRY_CAST({e} AS DOUBLE)) AS min_num,
                   median(TRY_CAST({e} AS DOUBLE)) AS median_num,
                   max(TRY_CAST({e} AS DOUBLE)) AS max_num,
                   any_value({e}) AS example
            FROM {table} WHERE {where} AND len(state) >= {i}
        """).fetchone()
        non_null, ok_bigint, ok_double, ok_bool, distinct, mn, med, mx, example = r
        inferred = infer_type(non_null, ok_bigint, ok_double, ok_bool)
        positions[i] = {
            "position": i,
            "expected_field": config.STATE_FIELDS[i - 1] if i - 1 < len(config.STATE_FIELDS) else None,
            "null_pct": round(100.0 * (n_rows - non_null) / n_rows, 1) if n_rows else None,
            "inferred_type": inferred,
            "distinct": distinct,
            "min": mn, "median": med, "max": mx,
            "example": example,
        }
    return {"rows": n_rows, "field_count": {"min": min_len, "max": max_len, "mode": mode_len}, "positions": positions}


def _ratio(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or b == 0:
        return None
    return a / b


def schema_diff(conn: duckdb.DuckDBPyConnection, current_run_id: str, baseline_run_id: str | None,
                table: str = "bronze_states") -> dict[str, Any]:
    """Compare the current run's per-position profile with a baseline (a previous good run, or all
    other rows in the table when none is known). Flags positions whose type, null rate or median moved."""
    cur = profile_positions(conn, run_id=current_run_id, table=table)
    base = profile_positions(conn, run_id=baseline_run_id, exclude_run_id=None if baseline_run_id else current_run_id, table=table)
    changes: list[dict[str, Any]] = []
    if cur["field_count"]["mode"] != base["field_count"]["mode"]:
        changes.append({"position": None, "kind": "field_count",
                        "baseline": base["field_count"]["mode"], "current": cur["field_count"]["mode"],
                        "note": "number of elements per state vector changed - later fields are likely shifted"})
    for i in sorted(set(cur["positions"]) | set(base["positions"])):
        c, b = cur["positions"].get(i), base["positions"].get(i)
        if c is None or b is None:
            changes.append({"position": i, "kind": "missing_or_new", "baseline": b and b["inferred_type"],
                            "current": c and c["inferred_type"]})
            continue
        if c["inferred_type"] != b["inferred_type"] and "null" not in (c["inferred_type"], b["inferred_type"]):
            changes.append({"position": i, "kind": "type", "baseline": b["inferred_type"], "current": c["inferred_type"]})
        if c["null_pct"] is not None and b["null_pct"] is not None and abs(c["null_pct"] - b["null_pct"]) > NULL_RATE_DELTA_PTS:
            changes.append({"position": i, "kind": "null_rate_pct", "baseline": b["null_pct"], "current": c["null_pct"]})
        r = _ratio(c["median"], b["median"])
        if r is not None and (r > MEDIAN_RATIO_LIMIT or r < 1 / MEDIAN_RATIO_LIMIT) and abs(b["median"]) > 1e-9:
            changes.append({"position": i, "kind": "median", "baseline": b["median"], "current": c["median"],
                            "ratio": round(r, 4)})
        elif r is not None and abs(r - 1) > 0.25 and abs(b["median"]) > 1e-9:
            changes.append({"position": i, "kind": "median_shift", "baseline": b["median"], "current": c["median"],
                            "ratio": round(r, 4)})
    return {"current_run_id": current_run_id, "baseline_run_id": baseline_run_id,
            "field_count": {"baseline": base["field_count"], "current": cur["field_count"]},
            "changes": changes, "current_profile": cur["positions"], "baseline_profile": base["positions"]}
