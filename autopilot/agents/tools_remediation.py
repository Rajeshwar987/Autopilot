"""Tool surface for the remediation agent: propose -> sandbox -> checks -> human approval -> prod -> report.
Prod is opened only inside `apply_to_prod`, and only after an `approved` row exists."""
from __future__ import annotations

import difflib
import json
import time
from dataclasses import dataclass, field
from typing import Callable

import duckdb

from autopilot import config, ui
from autopilot.agents import sqlguard
from autopilot.agents.llm import Tool
from autopilot.agents.schemas import Diagnosis
from autopilot.ops.store import Store
from autopilot.quality import engine
from autopilot.report import incident_report
from autopilot.warehouse import db, runner


@dataclass
class RemediationContext:
    store: Store
    sandbox: duckdb.DuckDBPyConnection  # read-write, hardened
    incident_id: str
    run_id: str
    previous_snapshot_time: int | None
    diagnosis: Diagnosis
    ask_human: Callable[[str], str] = input
    max_proposals: int = 3
    report_path: str | None = field(default=None)


def _diff(old: str, new: str, model_name: str) -> str:
    return "\n".join(difflib.unified_diff(
        old.splitlines(), new.splitlines(),
        fromfile=f"models/silver/{model_name}.sql (current)", tofile=f"models/silver/{model_name}.sql (proposed)",
        lineterm="", n=2))


def _gold_metric(conn: duckdb.DuckDBPyConnection, table: str, run_id: str) -> dict:
    row = conn.execute(
        f"SELECT count(*), count(*) FILTER (WHERE on_ground = false), "
        f"round(avg(velocity) FILTER (WHERE on_ground = false), 1), round(100.0 * count(latitude) / count(*), 1) "
        f"FROM {table} WHERE run_id = '{run_id}'").fetchone()
    return {"rows": row[0], "airborne": row[1], "mean_velocity_ms": row[2], "pct_with_position": row[3]}


def _check_summary(results: list[engine.CheckResult]) -> dict:
    s = engine.summarize(results)
    return {"passed": s["passed"], "total": s["total"], "failed": s["failed"]}


def build_tools(ctx: RemediationContext) -> list[Tool]:
    def get_model_sql(model_name: str = "silver_flights") -> str:
        try:
            return runner.read_model_sql(model_name)
        except FileNotFoundError as e:
            return f"ERROR: {e}. Models: {', '.join(runner.list_models())}"

    def run_sql_readonly(sql: str) -> str:
        return sqlguard.run_readonly(ctx.sandbox, sql)

    def propose_fix(model_name: str, new_model_sql: str, rationale: str, backfill_sql: str = "", rollback_sql: str = "") -> str:
        if len(ctx.store.proposals(ctx.incident_id)) >= ctx.max_proposals:
            return "ERROR: proposal limit reached for this incident; request approval for the best proposal or write the report"
        if model_name not in runner.list_models(("silver",)):
            return f"ERROR: only silver models may be changed: {runner.list_models(('silver',))}"
        try:
            sqlguard.validate_model_sql(new_model_sql, model_name)  # validates the comment-stripped form
            if backfill_sql.strip():
                sqlguard.validate_backfill(backfill_sql)
        except sqlguard.SQLGuardError as e:
            return f"ERROR: {e}"
        # store the agent's text as written (header comments included), normalised to one trailing ';'
        new_sql = new_model_sql.strip().rstrip(";").rstrip() + ";"
        old_sql = runner.read_model_sql(model_name)
        pid = ctx.store.save_proposal(ctx.incident_id, model_name, old_sql, new_sql, backfill_sql, rollback_sql, rationale)
        diff = _diff(old_sql, new_sql, model_name)
        ui.diff(diff, title=f"{pid}: proposed change to {model_name}")
        return f"proposal_id={pid}\n{diff}"

    def apply_to_sandbox(proposal_id: str) -> str:
        p = ctx.store.proposal(proposal_id)
        if not p:
            return f"ERROR: unknown proposal {proposal_id}"
        try:
            ctx.sandbox.execute("CREATE TABLE IF NOT EXISTS silver_flights_before AS SELECT * FROM silver_flights")
            ctx.sandbox.execute("BEGIN")
            rows = runner.run_models(ctx.sandbox, ("silver", "gold"), overrides={p["model_name"]: p["new_sql"]})
            for st in sqlguard.validate_backfill(p["backfill_sql"]) if (p["backfill_sql"] or "").strip() else []:
                ctx.sandbox.execute(st)
            ctx.sandbox.execute("COMMIT")
        except (duckdb.Error, sqlguard.SQLGuardError) as e:
            try:
                ctx.sandbox.execute("ROLLBACK")
            except duckdb.Error:
                pass
            ctx.store.mark_sandbox(proposal_id, False, {"error": str(e)})
            return f"ERROR applying {proposal_id} to sandbox: {e}"
        ctx.store.mark_sandbox(proposal_id, True, None)
        return f"applied {proposal_id} to sandbox. rows: {json.dumps(rows)}. Now call run_checks_on_sandbox."

    def run_checks_on_sandbox(proposal_id: str) -> str:
        p = ctx.store.proposal(proposal_id)
        if not p or not p["sandbox_applied"]:
            return f"ERROR: {proposal_id} has not been applied to the sandbox"
        cur = engine.run_checks(ctx.sandbox, ctx.run_id, {"previous_snapshot_time": ctx.previous_snapshot_time})
        hist_checks = [c for c in engine.load_checks() if c["type"] not in ("freshness", "row_count_min")]
        hist = engine.run_checks(ctx.sandbox, None, {}, checks=hist_checks)
        before = _gold_metric(ctx.sandbox, "silver_flights_before", ctx.run_id)
        after = _gold_metric(ctx.sandbox, "silver_flights", ctx.run_id)
        summary = {"failing_run": _check_summary(cur), "all_history": _check_summary(hist),
                   "metrics_before": before, "metrics_after": after}
        ctx.store.mark_sandbox(proposal_id, True, summary)
        ctx.store.record_check_results(ctx.run_id, [r.as_dict() for r in cur], target=f"sandbox:{proposal_id}")
        lines = [f"sandbox checks for {proposal_id}:",
                 f"- failing run {ctx.run_id}: {summary['failing_run']['passed']}/{summary['failing_run']['total']} passed; still failing: {summary['failing_run']['failed']}",
                 f"- all rows in sandbox (history): {summary['all_history']['passed']}/{summary['all_history']['total']} passed; failing: {summary['all_history']['failed']}",
                 f"- metrics for the failing run BEFORE: {before}", f"- metrics AFTER: {after}"]
        for r in cur + hist:
            if r.status != "passed":
                lines.append(f"  {r.check_name}: {r.failed_rows}/{r.total_rows} {r.details.get('reason') or r.details.get('stats') or r.details.get('error') or ''}")
        return "\n".join(lines)

    def request_human_approval(proposal_id: str) -> str:
        existing = ctx.store.approval(proposal_id)
        if existing:
            return f"{existing['decision']}: {existing['note'] or ''}".strip()
        p = ctx.store.proposal(proposal_id)
        if not p:
            return f"ERROR: unknown proposal {proposal_id}"
        summary = p["sandbox_check_summary"]
        summary = json.loads(summary) if isinstance(summary, str) else summary
        if not summary or "failing_run" not in summary:
            return "ERROR: run_checks_on_sandbox first"
        ui.banner("HUMAN APPROVAL REQUIRED", style="bold yellow")
        ui.diff(_diff(p["old_sql"], p["new_sql"], p["model_name"]), title=f"{proposal_id}: {p['model_name']}")
        if (p["backfill_sql"] or "").strip():
            ui.sql(p["backfill_sql"], title="backfill")
        ui.kv("Agent rationale", {"rationale": p["rationale"]})
        ui.kv("Sandbox results", summary)
        target = "MotherDuck" if db.is_motherduck() else config.warehouse_url()
        answer = ctx.ask_human(f"Apply {proposal_id} to prod ({target})? [y/N/note]: ").strip()
        if answer.lower() in ("y", "yes"):
            ctx.store.save_approval(proposal_id, "approved", "")
            return "approved"
        note = "" if answer.lower() in ("", "n", "no") else answer
        ctx.store.save_approval(proposal_id, "declined", note)
        return f"declined: {note}" if note else "declined"

    def apply_to_prod(proposal_id: str) -> str:
        p = ctx.store.proposal(proposal_id)
        a = ctx.store.approval(proposal_id)
        if not p or not a or a["decision"] != "approved":
            return f"ERROR: {proposal_id} has no recorded human approval; call request_human_approval"
        summary = p["sandbox_check_summary"]
        summary = json.loads(summary) if isinstance(summary, str) else summary
        if summary.get("failing_run", {}).get("failed") and "force" not in (a["note"] or "").lower():
            return "ERROR: sandbox checks for the failing run are not all passing; fix the proposal first"
        model_path = runner.model_path(p["model_name"])
        backup = config.SANDBOX_DIR / f"{p['model_name']}.{int(time.time())}.sql.bak"
        backup.write_text(p["old_sql"], encoding="utf-8")
        runner.write_model_sql(p["model_name"], p["new_sql"])
        prod = db.connect()
        try:
            prod.execute("BEGIN")
            rows = runner.run_models(prod, ("silver", "gold"))
            for st in sqlguard.validate_backfill(p["backfill_sql"]) if (p["backfill_sql"] or "").strip() else []:
                prod.execute(st)
            prod.execute("COMMIT")
            results = engine.run_checks(prod, ctx.run_id, {"previous_snapshot_time": ctx.previous_snapshot_time})
        except (duckdb.Error, sqlguard.SQLGuardError) as e:
            try:
                prod.execute("ROLLBACK")
            except duckdb.Error:
                pass
            runner.write_model_sql(p["model_name"], p["old_sql"])  # restore the file
            return f"ERROR applying to prod (rolled back, model file restored): {e}"
        finally:
            prod.close()
        ctx.store.record_check_results(ctx.run_id, [r.as_dict() for r in results], target="prod_after_fix")
        s = _check_summary(results)
        ctx.store.set_incident(ctx.incident_id, status="applied")
        ui.ok(f"applied {proposal_id} to prod: {s['passed']}/{s['total']} checks pass; {model_path.name} updated (backup {backup.name})")
        return f"applied to prod. model file {model_path.as_posix()} updated. rows: {json.dumps(rows)}. prod checks: {s}"

    def write_incident_report(narrative_md: str) -> str:
        path = incident_report.write(ctx.store, ctx.incident_id, narrative_md)
        ctx.report_path = path.as_posix()
        inc = ctx.store.incident(ctx.incident_id)
        final = "closed_applied" if inc and inc["status"] == "applied" else "closed_declined"
        ctx.store.set_incident(ctx.incident_id, status=final, close=True)
        return f"report written: {path.as_posix()}"

    S = {"type": "string"}
    return [
        Tool("get_model_sql", "Current SQL of a model file (default silver_flights).", {"properties": {"model_name": S}}, get_model_sql),
        Tool("run_sql_readonly", "Run one read-only SQL statement on the sandbox (50-row cap).", {"properties": {"sql": S}, "required": ["sql"]}, run_sql_readonly),
        Tool("propose_fix", "Register a proposed fix: the complete new SQL of one silver model plus rationale (and optional backfill/rollback SQL). Returns a proposal_id and a diff.",
             {"properties": {"model_name": S, "new_model_sql": S, "rationale": S, "backfill_sql": S, "rollback_sql": S},
              "required": ["model_name", "new_model_sql", "rationale"]}, propose_fix),
        Tool("apply_to_sandbox", "Execute a proposal in the sandbox copy (never prod).", {"properties": {"proposal_id": S}, "required": ["proposal_id"]}, apply_to_sandbox),
        Tool("run_checks_on_sandbox", "Re-run every quality check in the sandbox for the failing run AND for all historical rows; returns before/after metrics.",
             {"properties": {"proposal_id": S}, "required": ["proposal_id"]}, run_checks_on_sandbox),
        Tool("request_human_approval", "Show the diff and sandbox results to the human operator and ask for approval to apply to prod. Returns 'approved' or 'declined[: note]'.",
             {"properties": {"proposal_id": S}, "required": ["proposal_id"]}, request_human_approval),
        Tool("apply_to_prod", "Apply an APPROVED proposal to prod: updates the model file, rebuilds silver/gold, re-runs checks. Refuses without approval.",
             {"properties": {"proposal_id": S}, "required": ["proposal_id"]}, apply_to_prod),
        Tool("write_incident_report", "Write the incident report (Markdown narrative; the harness appends timeline, diff and check tables) and close the incident. Ends the run.",
             {"properties": {"narrative_md": S}, "required": ["narrative_md"]}, write_incident_report, terminal=True),
    ]
