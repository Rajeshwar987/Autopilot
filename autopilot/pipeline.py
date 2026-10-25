"""One pipeline run: fetch (or replay) -> optional chaos -> bronze -> silver/gold -> checks -> incident."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from autopilot import config, ui
from autopilot.chaos import faults
from autopilot.ingest import opensky
from autopilot.ops.store import Store, new_run_id
from autopilot.quality import engine
from autopilot.warehouse import db, runner


@dataclass
class RunOutcome:
    run_id: str
    snapshot_time: int
    rows: int
    checks: list[dict] = field(default_factory=list)
    failing: list[str] = field(default_factory=list)
    incident_id: str | None = None
    chaos_fault: str | None = None


def _mode_field_count(payload: dict) -> int | None:
    counts: dict[int, int] = {}
    for s in payload.get("states") or []:
        counts[len(s)] = counts.get(len(s), 0) + 1
    return max(counts, key=counts.get) if counts else None


def run_once(store: Store, chaos: str | None = None, replay: Path | None = None) -> RunOutcome:
    run_id = new_run_id()
    ui.banner(f"Autopilot run {run_id}")

    prev = store.recent_ingest_runs(1)
    previous_snapshot_time = prev[0]["snapshot_time"] if prev else None

    # 1. fetch or replay
    source = "opensky"
    if replay:
        payload = opensky.load_raw(replay)
        # replayed snapshots get a fresh clock, guaranteed to advance past the previous run
        fresh = int(time.time()) // 10 * 10
        if previous_snapshot_time is not None and previous_snapshot_time < 2_000_000_000:  # ignore ms-faulted runs
            fresh = max(fresh, int(previous_snapshot_time) + 10)
        payload["time"] = fresh
        source = "replay"
        ui.info(f"replaying {Path(replay).name} ({len(payload['states'])} aircraft)")
    else:
        try:
            payload = opensky.fetch_states()
            ui.info(f"fetched {len(payload['states'])} aircraft over bbox {config.opensky_bbox()} at {payload['time']}")
        except opensky.OpenSkyError as e:
            last = opensky.latest_raw_path()
            if last is None:
                raise
            ui.warn(f"{e} - falling back to last saved snapshot {last.name}")
            payload, source = opensky.load_raw(last), "fallback"

    # 2. chaos (labelled)
    if chaos:
        ctx = faults.ChaosContext(previous_loader=lambda: (lambda p: opensky.load_raw(p) if p else None)(opensky.latest_raw_path()))
        payload = faults.apply_fault(chaos, payload, ctx)
        ui.chaos_banner(chaos, faults.describe(chaos))
    snap = opensky.save_raw(payload, chaos_fault=chaos, source=source)

    # 3. load + models
    conn = db.connect()
    try:
        runner.run_models(conn, ("bronze",))
        rows = db.load_bronze(conn, run_id, payload)
        model_rows = runner.run_models(conn, ("silver", "gold"))
        ui.info(f"bronze +{rows} rows; " + "; ".join(f"{k}={v}" for k, v in model_rows.items()))
        metrics = conn.execute(
            "SELECT airborne, mean_velocity_ms, pct_with_position FROM gold_snapshot_metrics "
            "ORDER BY snapshot_time DESC LIMIT 1").fetchone()
        if metrics:
            ui.info(f"gold: airborne={metrics[0]}  mean airborne velocity={metrics[1]} m/s  with_position={metrics[2]}%")
        store.record_ingest_run(run_id, int(payload["time"]), rows, _mode_field_count(payload), source, chaos,
                                "checking", snap.path.as_posix())

        # 4. checks
        results = engine.run_checks(conn, run_id, {"previous_snapshot_time": previous_snapshot_time})
    finally:
        conn.close()
    dicts = [r.as_dict() for r in results]
    store.record_check_results(run_id, dicts)
    summary = engine.summarize(results)
    ui.check_table(dicts, title=f"Quality checks: {summary['passed']}/{summary['total']} passed")

    outcome = RunOutcome(run_id, int(payload["time"]), rows, dicts, summary["failed"], chaos_fault=chaos)
    if not summary["failed"]:
        store.set_run_status(run_id, "passed")
        ui.ok("all checks passed")
        return outcome

    # 5. incident
    store.set_run_status(run_id, "failed")
    outcome.incident_id = store.open_incident(run_id, summary["failed"])
    for r in results:
        if r.status != "passed":
            ui.fail(f"{r.check_name}: {r.failed_rows}/{r.total_rows} rows  {r.details.get('reason') or r.details.get('stats') or ''}")
    ui.warn(f"incident {outcome.incident_id} opened")
    return outcome
