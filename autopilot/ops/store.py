"""Incident store (ops.duckdb): ingest runs, check results, incidents, agent runs, diagnoses,
fix proposals, approvals. Kept local even when prod is MotherDuck - it is written constantly by the
loop and the agents. `ingest_runs.chaos_fault` is the honesty label; agent tools never select it."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb

from autopilot import config

DDL = """
CREATE TABLE IF NOT EXISTS ingest_runs (
    run_id VARCHAR PRIMARY KEY, started_at TIMESTAMP, snapshot_time BIGINT, row_count INTEGER,
    field_count_mode INTEGER, source VARCHAR, chaos_fault VARCHAR, status VARCHAR, raw_path VARCHAR
);
CREATE TABLE IF NOT EXISTS check_results (
    run_id VARCHAR, check_name VARCHAR, table_name VARCHAR, status VARCHAR, failed_rows BIGINT,
    total_rows BIGINT, sql_text VARCHAR, details JSON, checked_at TIMESTAMP, target VARCHAR
);
CREATE TABLE IF NOT EXISTS incidents (
    incident_id VARCHAR PRIMARY KEY, run_id VARCHAR, opened_at TIMESTAMP, status VARCHAR,
    failing_checks JSON, sandbox_path VARCHAR, closed_at TIMESTAMP
);
CREATE TABLE IF NOT EXISTS agent_runs (
    agent_run_id VARCHAR PRIMARY KEY, incident_id VARCHAR, agent VARCHAR, model VARCHAR,
    started_at TIMESTAMP, ended_at TIMESTAMP, input_tokens BIGINT, output_tokens BIGINT,
    tool_calls INTEGER, status VARCHAR, transcript JSON
);
CREATE TABLE IF NOT EXISTS diagnoses (
    incident_id VARCHAR, fault_type VARCHAR, affected_table VARCHAR, affected_columns JSON,
    root_cause VARCHAR, evidence JSON, confidence DOUBLE, fixable BOOLEAN,
    suggested_fix_strategy VARCHAR, created_at TIMESTAMP
);
CREATE TABLE IF NOT EXISTS fix_proposals (
    proposal_id VARCHAR PRIMARY KEY, incident_id VARCHAR, model_name VARCHAR, old_sql VARCHAR,
    new_sql VARCHAR, backfill_sql VARCHAR, rollback_sql VARCHAR, rationale VARCHAR,
    sandbox_applied BOOLEAN DEFAULT false, sandbox_check_summary JSON, created_at TIMESTAMP
);
CREATE TABLE IF NOT EXISTS approvals (
    proposal_id VARCHAR PRIMARY KEY, decided_at TIMESTAMP, decision VARCHAR, decided_by VARCHAR, note VARCHAR
);
"""


def now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def new_run_id() -> str:
    return "run-" + now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


class Store:
    def __init__(self, path: Path | None = None):
        self.path = path or config.OPS_DB
        self.conn = duckdb.connect(self.path.as_posix())
        for stmt in DDL.strip().split(";"):
            if stmt.strip():
                self.conn.execute(stmt)

    def close(self) -> None:
        self.conn.close()

    # ---- helpers ----
    def _rows(self, sql: str, params: list | None = None) -> list[dict[str, Any]]:
        cur = self.conn.execute(sql, params or [])
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def _next_id(self, table: str, prefix: str) -> str:
        n = self.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        return f"{prefix}-{n + 1:04d}"

    # ---- ingest runs ----
    def record_ingest_run(self, run_id: str, snapshot_time: int, row_count: int, field_count_mode: int | None,
                          source: str, chaos_fault: str | None, status: str, raw_path: str | None) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO ingest_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [run_id, now(), snapshot_time, row_count, field_count_mode, source, chaos_fault, status, raw_path],
        )

    def recent_ingest_runs(self, n: int = 10) -> list[dict[str, Any]]:
        # NOTE: deliberately excludes chaos_fault and raw_path - this is what agent tools see.
        return self._rows(
            "SELECT run_id, started_at, snapshot_time, row_count, field_count_mode, source, status "
            "FROM ingest_runs ORDER BY started_at DESC LIMIT ?", [int(n)])

    def ingest_run(self, run_id: str) -> dict[str, Any] | None:
        rows = self._rows("SELECT * FROM ingest_runs WHERE run_id = ?", [run_id])
        return rows[0] if rows else None

    def last_good_run(self, before_run_id: str) -> dict[str, Any] | None:
        rows = self._rows(
            "SELECT run_id, snapshot_time FROM ingest_runs WHERE status = 'passed' AND started_at < "
            "(SELECT started_at FROM ingest_runs WHERE run_id = ?) ORDER BY started_at DESC LIMIT 1",
            [before_run_id])
        return rows[0] if rows else None

    def set_run_status(self, run_id: str, status: str) -> None:
        self.conn.execute("UPDATE ingest_runs SET status = ? WHERE run_id = ?", [status, run_id])

    # ---- check results ----
    def record_check_results(self, run_id: str, results: list[dict[str, Any]], target: str = "prod") -> None:
        for r in results:
            self.conn.execute(
                "INSERT INTO check_results VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [run_id, r["check_name"], r["table_name"], r["status"], r.get("failed_rows"),
                 r.get("total_rows"), r.get("sql_text"), json.dumps(r.get("details", {})), now(), target])

    def check_results(self, run_id: str, target: str = "prod") -> list[dict[str, Any]]:
        return self._rows(
            "SELECT check_name, table_name, status, failed_rows, total_rows, sql_text, details "
            "FROM check_results WHERE run_id = ? AND target = ? ORDER BY status, check_name", [run_id, target])

    # ---- incidents ----
    def open_incident(self, run_id: str, failing_checks: list[str]) -> str:
        incident_id = self._next_id("incidents", "INC")
        self.conn.execute(
            "INSERT INTO incidents VALUES (?, ?, ?, 'open', ?, NULL, NULL)",
            [incident_id, run_id, now(), json.dumps(failing_checks)])
        return incident_id

    def set_incident(self, incident_id: str, status: str | None = None, sandbox_path: str | None = None,
                     close: bool = False) -> None:
        if status:
            self.conn.execute("UPDATE incidents SET status = ? WHERE incident_id = ?", [status, incident_id])
        if sandbox_path:
            self.conn.execute("UPDATE incidents SET sandbox_path = ? WHERE incident_id = ?",
                              [sandbox_path, incident_id])
        if close:
            self.conn.execute("UPDATE incidents SET closed_at = ? WHERE incident_id = ?", [now(), incident_id])

    def incident(self, incident_id: str) -> dict[str, Any] | None:
        rows = self._rows("SELECT * FROM incidents WHERE incident_id = ?", [incident_id])
        return rows[0] if rows else None

    def incidents(self, n: int = 20) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM incidents ORDER BY opened_at DESC LIMIT ?", [int(n)])

    # ---- agent runs ----
    def record_agent_run(self, incident_id: str, agent: str, model: str, started_at: datetime,
                         input_tokens: int, output_tokens: int, tool_calls: int, status: str,
                         transcript: list[dict]) -> str:
        agent_run_id = uuid.uuid4().hex[:12]
        self.conn.execute(
            "INSERT INTO agent_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [agent_run_id, incident_id, agent, model, started_at, now(), input_tokens, output_tokens,
             tool_calls, status, json.dumps(transcript, default=str)])
        return agent_run_id

    def agent_runs(self, incident_id: str) -> list[dict[str, Any]]:
        return self._rows(
            "SELECT agent, model, started_at, ended_at, input_tokens, output_tokens, tool_calls, status "
            "FROM agent_runs WHERE incident_id = ? ORDER BY started_at", [incident_id])

    # ---- diagnoses ----
    def save_diagnosis(self, incident_id: str, d: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO diagnoses VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [incident_id, d["fault_type"], d["affected_table"], json.dumps(d["affected_columns"]),
             d["root_cause"], json.dumps(d["evidence"]), float(d["confidence"]), bool(d["fixable"]),
             d.get("suggested_fix_strategy", ""), now()])

    def diagnosis(self, incident_id: str) -> dict[str, Any] | None:
        rows = self._rows(
            "SELECT * FROM diagnoses WHERE incident_id = ? ORDER BY created_at DESC LIMIT 1", [incident_id])
        return rows[0] if rows else None

    # ---- proposals ----
    def save_proposal(self, incident_id: str, model_name: str, old_sql: str, new_sql: str,
                      backfill_sql: str, rollback_sql: str, rationale: str) -> str:
        proposal_id = self._next_id("fix_proposals", "FIX")
        self.conn.execute(
            "INSERT INTO fix_proposals VALUES (?, ?, ?, ?, ?, ?, ?, ?, false, NULL, ?)",
            [proposal_id, incident_id, model_name, old_sql, new_sql, backfill_sql, rollback_sql,
             rationale, now()])
        return proposal_id

    def proposal(self, proposal_id: str) -> dict[str, Any] | None:
        rows = self._rows("SELECT * FROM fix_proposals WHERE proposal_id = ?", [proposal_id])
        return rows[0] if rows else None

    def proposals(self, incident_id: str) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM fix_proposals WHERE incident_id = ? ORDER BY created_at", [incident_id])

    def mark_sandbox(self, proposal_id: str, applied: bool, summary: dict | None = None) -> None:
        self.conn.execute(
            "UPDATE fix_proposals SET sandbox_applied = ?, sandbox_check_summary = ? WHERE proposal_id = ?",
            [applied, json.dumps(summary) if summary is not None else None, proposal_id])

    # ---- approvals ----
    def save_approval(self, proposal_id: str, decision: str, note: str, decided_by: str = "cli") -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO approvals VALUES (?, ?, ?, ?, ?)",
            [proposal_id, now(), decision, decided_by, note])

    def approval(self, proposal_id: str) -> dict[str, Any] | None:
        rows = self._rows("SELECT * FROM approvals WHERE proposal_id = ?", [proposal_id])
        return rows[0] if rows else None
