"""Incident handling: sandbox -> diagnosis agent -> (if fixable) remediation agent -> report."""
from __future__ import annotations

import time
from datetime import datetime

import duckdb

from autopilot import ui
from autopilot.agents import llm, prompts, tools_diagnosis, tools_remediation
from autopilot.agents.schemas import Diagnosis
from autopilot.ops.store import Store
from autopilot.report import incident_report
from autopilot.warehouse import db


def _log(store: Store, incident_id: str, agent: str, res: llm.AgentResult) -> None:
    store.record_agent_run(incident_id, agent, res.model, res.started_at, res.input_tokens, res.output_tokens,
                           res.tool_calls, res.finished_by, res.messages)


def handle_incident(store: Store, incident_id: str, ask_human=input) -> str | None:
    """Run the full agentic loop for an open incident. Returns the report path (or None)."""
    inc = store.incident(incident_id)
    if not inc:
        raise ValueError(f"unknown incident {incident_id}")
    run_id = inc["run_id"]
    failing = inc["failing_checks"]
    failing = failing if isinstance(failing, list) else __import__("json").loads(failing)
    baseline = store.last_good_run(run_id)
    baseline_run_id = baseline["run_id"] if baseline else None
    prev_runs = [r for r in store.recent_ingest_runs(5) if r["run_id"] != run_id]
    previous_snapshot_time = prev_runs[0]["snapshot_time"] if prev_runs else None

    # 1. sandbox
    store.set_incident(incident_id, status="diagnosing")
    t0 = time.time()
    sb_path = db.create_sandbox(incident_id)
    store.set_incident(incident_id, sandbox_path=sb_path.as_posix())
    ui.info(f"sandbox {sb_path.name} created from prod (last hour) in {time.time() - t0:.1f}s")

    # 2. diagnosis (read-only connection)
    ro = db.harden(duckdb.connect(sb_path.as_posix(), read_only=True))
    try:
        dctx = tools_diagnosis.DiagnosisContext(store, ro, incident_id, run_id, baseline_run_id)
        user = (f"Incident {incident_id}. Ingest run `{run_id}` failed these checks: {', '.join(failing)}.\n"
                f"Baseline (last good) run: {baseline_run_id or 'unknown - compare against all other rows'}.\n"
                f"Sandbox tables: bronze_states, silver_flights (both contain run_id). Investigate and record a diagnosis.")
        res = llm.run_agent("Diagnosis agent", prompts.DIAGNOSIS_SYSTEM, user, tools_diagnosis.build_tools(dctx))
        _log(store, incident_id, "diagnosis", res)
    finally:
        ro.close()
    diagnosis: Diagnosis | None = dctx.diagnosis
    if diagnosis is None:
        ui.fail("diagnosis agent did not record a diagnosis; incident left open")
        store.set_incident(incident_id, status="open")
        return None
    ui.kv("Diagnosis", diagnosis.model_dump())

    # 3. not fixable -> report without remediation
    if not diagnosis.fixable:
        narrative = (f"The diagnosis agent concluded this incident is **not fixable with SQL** "
                     f"(`{diagnosis.fault_type}`): {diagnosis.root_cause}\n\n"
                     f"Recommended action: {diagnosis.suggested_fix_strategy or 'alert the upstream owner; do not transform data.'}")
        path = incident_report.write(store, incident_id, narrative)
        store.set_incident(incident_id, status="closed_no_fix", close=True)
        ui.warn(f"not fixable - report written: {path.as_posix()}")
        return path.as_posix()

    # 4. remediation (read-write sandbox)
    store.set_incident(incident_id, status="proposing")
    rw = db.harden(duckdb.connect(sb_path.as_posix()))
    try:
        rctx = tools_remediation.RemediationContext(store, rw, incident_id, run_id, previous_snapshot_time,
                                                    diagnosis, ask_human=ask_human)
        user = (f"Incident {incident_id}, failing run `{run_id}`, failing checks: {', '.join(failing)}.\n"
                f"Diagnosis:\n{diagnosis.model_dump_json(indent=2)}\n\n"
                f"Sandbox tables: bronze_states, silver_flights. Follow the required sequence.")
        res = llm.run_agent("Remediation agent", prompts.REMEDIATION_SYSTEM, user, tools_remediation.build_tools(rctx))
        _log(store, incident_id, "remediation", res)
    finally:
        rw.close()

    if rctx.report_path is None:
        # agent stopped without writing the report - the harness writes a minimal one
        narrative = res.final_text or "The remediation agent ended without writing a report."
        path = incident_report.write(store, incident_id, narrative)
        inc = store.incident(incident_id) or {}
        store.set_incident(incident_id, status="closed_applied" if inc.get("status") == "applied" else "closed_incomplete", close=True)
        rctx.report_path = path.as_posix()
    ui.ok(f"incident {incident_id} closed - report {rctx.report_path}")
    return rctx.report_path
