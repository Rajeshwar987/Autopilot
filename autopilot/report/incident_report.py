"""Assemble reports/INC-xxxx.md from the incident store. The agent supplies only the narrative; the
timeline, diagnosis, diff, check tables, token cost and the chaos disclosure come from the store."""
from __future__ import annotations

import difflib
import json
from pathlib import Path
from typing import Any

from autopilot import config
from autopilot.ops.store import Store


def _j(v: Any) -> Any:
    if isinstance(v, str):
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return v
    return v


def _check_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "_none_"
    out = ["| check | status | failed/total | detail |", "|---|---|---:|---|"]
    for r in rows:
        det = _j(r.get("details")) or {}
        detail = det.get("reason") or det.get("stats") or det.get("error") or ""
        out.append(f"| {r['check_name']} | {r['status']} | {r.get('failed_rows')}/{r.get('total_rows')} | {str(detail)[:80]} |")
    return "\n".join(out)


def write(store: Store, incident_id: str, narrative_md: str) -> Path:
    inc = store.incident(incident_id) or {}
    run = store.ingest_run(inc.get("run_id", "")) or {}
    diag = store.diagnosis(incident_id)
    props = store.proposals(incident_id)
    agent_runs = store.agent_runs(incident_id)
    before = store.check_results(inc.get("run_id", ""), "prod")
    after = store.check_results(inc.get("run_id", ""), "prod_after_fix")

    md: list[str] = [f"# Incident {incident_id}", ""]
    md += [f"- **Opened:** {inc.get('opened_at')}  ",
           f"- **Run:** `{inc.get('run_id')}` snapshot_time={run.get('snapshot_time')} rows={run.get('row_count')} source={run.get('source')}  ",
           f"- **Failing checks:** {', '.join(_j(inc.get('failing_checks')) or [])}  ",
           f"- **Status:** {inc.get('status')}", ""]
    md += ["## Summary (written by the remediation agent)", "", narrative_md.strip(), ""]

    if diag:
        md += ["## Diagnosis (diagnosis agent)", "",
               f"- **Fault type:** `{diag['fault_type']}` (confidence {diag['confidence']:.2f}, fixable={diag['fixable']})  ",
               f"- **Affected:** {diag['affected_table']} -> {', '.join(_j(diag['affected_columns']) or [])}  ",
               f"- **Root cause:** {diag['root_cause']}", "", "Evidence:", ""]
        md += [f"- {e}" for e in (_j(diag["evidence"]) or [])]
        if diag.get("suggested_fix_strategy"):
            md += ["", f"Suggested strategy: {diag['suggested_fix_strategy']}"]
        md.append("")

    md += ["## Checks on the failing run (before)", "", _check_table(before), ""]

    for p in props:
        appr = store.approval(p["proposal_id"])
        summary = _j(p.get("sandbox_check_summary")) or {}
        decision = appr["decision"] if appr else "not requested"
        md += [f"## Proposal {p['proposal_id']} - {p['model_name']} ({decision})", "", f"**Rationale:** {p['rationale']}", ""]
        diff = "\n".join(difflib.unified_diff(p["old_sql"].splitlines(), p["new_sql"].splitlines(),
                                              fromfile="current", tofile="proposed", lineterm="", n=2))
        md += ["```diff", diff, "```", ""]
        if (p.get("backfill_sql") or "").strip():
            md += ["Backfill:", "", "```sql", p["backfill_sql"], "```", ""]
        if summary:
            fr, hist = summary.get("failing_run", {}), summary.get("all_history", {})
            md += [f"Sandbox: failing run {fr.get('passed')}/{fr.get('total')} checks passed; "
                   f"history {hist.get('passed')}/{hist.get('total')} passed.  ",
                   f"Metrics before: `{summary.get('metrics_before')}`  ", f"Metrics after: `{summary.get('metrics_after')}`", ""]
        if appr and appr.get("note"):
            md += [f"Operator note: {appr['note']}", ""]

    if after:
        md += ["## Checks on prod after the fix", "", _check_table(after), ""]

    if agent_runs:
        tot_in = sum(r["input_tokens"] or 0 for r in agent_runs)
        tot_out = sum(r["output_tokens"] or 0 for r in agent_runs)
        md += ["## Agent runs", "", "| agent | model | tool calls | tokens in/out | status |", "|---|---|---:|---:|---|"]
        md += [f"| {r['agent']} | {r['model']} | {r['tool_calls']} | {r['input_tokens']}/{r['output_tokens']} | {r['status']} |"
               for r in agent_runs]
        md += ["", f"Total tokens: {tot_in} in / {tot_out} out.", ""]

    md += ["---", ""]
    if run.get("chaos_fault"):
        md += [f"**Disclosure:** this incident was produced by Autopilot's chaos module (`{run['chaos_fault']}`), "
               "which injects a realistic upstream fault into a live OpenSky snapshot. The agents had no access to "
               "this label; they diagnosed and fixed it from the data alone."]
    else:
        md += ["**Disclosure:** no chaos fault was injected for this run; the failure came from the live upstream data."]

    config.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = config.REPORTS_DIR / f"{incident_id}.md"
    path.write_text("\n".join(md) + "\n", encoding="utf-8")
    return path
