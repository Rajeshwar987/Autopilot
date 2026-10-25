"""Read-only tool surface for the diagnosis agent. The connection is a read-only, hardened sandbox;
the tool list contains no write operation at all - that is the guardrail."""
from __future__ import annotations

import json
from dataclasses import dataclass

import duckdb
from pydantic import ValidationError

from autopilot.agents import sqlguard
from autopilot.agents.llm import Tool
from autopilot.agents.schemas import Diagnosis
from autopilot.ops.store import Store
from autopilot.quality import profile
from autopilot.warehouse import runner


@dataclass
class DiagnosisContext:
    store: Store
    sandbox: duckdb.DuckDBPyConnection  # read_only=True, hardened
    incident_id: str
    run_id: str
    baseline_run_id: str | None
    diagnosis: Diagnosis | None = None


def build_tools(ctx: DiagnosisContext) -> list[Tool]:
    def get_check_results(run_id: str = "") -> str:
        rid = run_id or ctx.run_id
        rows = ctx.store.check_results(rid)
        if not rows:
            return f"no check results for run {rid}"
        out = [f"check results for run {rid} (status, failed/total, details):"]
        for r in rows:
            det = r["details"] if isinstance(r["details"], dict) else json.loads(r["details"] or "{}")
            out.append(f"- {r['check_name']} [{r['status']}] {r['failed_rows']}/{r['total_rows']} "
                       f"{det.get('reason') or det.get('stats') or ''} {('- ' + det['description']) if det.get('description') else ''}")
            if r["status"] != "passed":
                out.append(f"    sql: {r['sql_text']}")
        return "\n".join(out)

    def get_schema_diff(table: str = "bronze_states") -> str:
        d = profile.schema_diff(ctx.sandbox, ctx.run_id, ctx.baseline_run_id, table=table)
        lines = [f"schema diff: failing run {ctx.run_id} vs baseline {d['baseline_run_id'] or 'all other rows'}",
                 f"field_count per vector: baseline={d['field_count']['baseline']} current={d['field_count']['current']}",
                 "CHANGES:"]
        if not d["changes"]:
            lines.append("  (no position-level changes detected)")
        for c in d["changes"]:
            lines.append("  " + json.dumps(c, default=str))
        lines.append("CURRENT per-position profile (1-based index, expected field per silver model):")
        for i, p in d["current_profile"].items():
            lines.append(f"  [{i:2d}] {p['expected_field'] or '?':16s} type={p['inferred_type']:8s} null%={p['null_pct']} "
                         f"median={p['median']} example={str(p['example'])[:20]!r}")
        return "\n".join(lines)

    def sample_rows(table: str, where: str = "", n: int = 10) -> str:
        n = max(1, min(int(n), 20))
        w = sqlguard.validate_where_fragment(where)
        if table not in {"bronze_states", "silver_flights"}:
            return "ERROR: table must be bronze_states or silver_flights"
        sql = f"SELECT * FROM {table} WHERE run_id = '{ctx.run_id}' AND ({w}) LIMIT {n}"
        cur = sqlguard.execute_with_timeout(ctx.sandbox, sql, 10)
        return sqlguard.format_rows(cur, n)

    def run_sql_readonly(sql: str) -> str:
        return sqlguard.run_readonly(ctx.sandbox, sql)

    def get_recent_ingest_runs(n: int = 10) -> str:
        rows = ctx.store.recent_ingest_runs(max(1, min(int(n), 30)))
        out = ["run_id | started_at | snapshot_time | rows | field_count_mode | status"]
        out += [f"{r['run_id']} | {r['started_at']} | {r['snapshot_time']} | {r['row_count']} | "
                f"{r['field_count_mode']} | {r['status']}" for r in rows]
        return "\n".join(out)

    def get_model_sql(model_name: str = "silver_flights") -> str:
        try:
            return runner.read_model_sql(model_name)
        except FileNotFoundError as e:
            return f"ERROR: {e}. Models: {', '.join(runner.list_models())}"

    def record_diagnosis(fault_type: str, affected_table: str, affected_columns: list[str], root_cause: str,
                         evidence: list[str], confidence: float, fixable: bool, suggested_fix_strategy: str = "") -> str:
        try:
            d = Diagnosis(fault_type=fault_type, affected_table=affected_table, affected_columns=affected_columns,
                          root_cause=root_cause, evidence=evidence, confidence=confidence, fixable=fixable,
                          suggested_fix_strategy=suggested_fix_strategy)
        except ValidationError as e:
            return f"ERROR: invalid diagnosis: {e}"
        ctx.diagnosis = d
        ctx.store.save_diagnosis(ctx.incident_id, d.model_dump())
        return "recorded: " + d.model_dump_json()

    S = {"type": "string"}
    return [
        Tool("get_check_results", "Quality-check results for an ingest run (default: the failing run), including the SQL of each failed check.",
             {"properties": {"run_id": S}}, get_check_results),
        Tool("get_schema_diff", "Per-position profile of the raw state vectors (type, null %, median, field count) for the failing run vs the last good run, with detected changes.",
             {"properties": {"table": {**S, "description": "bronze_states (default)"}}}, get_schema_diff),
        Tool("sample_rows", "Sample up to 20 rows of bronze_states or silver_flights from the failing run, with an optional SQL WHERE fragment.",
             {"properties": {"table": S, "where": S, "n": {"type": "integer"}}, "required": ["table"]}, sample_rows),
        Tool("run_sql_readonly", "Run a single read-only SQL statement (SELECT/WITH/DESCRIBE/SUMMARIZE) on the sandbox. Results capped at 50 rows.",
             {"properties": {"sql": S}, "required": ["sql"]}, run_sql_readonly),
        Tool("get_recent_ingest_runs", "Recent ingest runs: snapshot_time, row count, field-count mode, pass/fail status.",
             {"properties": {"n": {"type": "integer"}}}, get_recent_ingest_runs),
        Tool("get_model_sql", "Current SQL of a model file (default silver_flights).",
             {"properties": {"model_name": S}}, get_model_sql),
        Tool("record_diagnosis", "Record the final diagnosis. Call exactly once when you are confident. Ends the investigation.",
             {"properties": {
                 "fault_type": S, "affected_table": S,
                 "affected_columns": {"type": "array", "items": S},
                 "root_cause": S,
                 "evidence": {"type": "array", "items": S, "description": "concrete numbers/observations from tool outputs"},
                 "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                 "fixable": {"type": "boolean"},
                 "suggested_fix_strategy": S},
              "required": ["fault_type", "affected_table", "affected_columns", "root_cause", "evidence", "confidence", "fixable"]},
             record_diagnosis, terminal=True),
    ]
