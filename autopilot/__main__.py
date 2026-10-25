"""CLI:  py -m autopilot run [--chaos NAME] [--replay FILE] [--no-agent]
        py -m autopilot loop [--interval 60] [--chaos NAME]
        py -m autopilot incident INC-0001        (re-run the agents on an existing incident)
        py -m autopilot incidents | faults | report INC-0001"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from autopilot import config, ui
from autopilot.chaos import faults
from autopilot.ops.store import Store
from autopilot.pipeline import run_once


def cmd_run(args: argparse.Namespace) -> int:
    store = Store()
    try:
        outcome = run_once(store, chaos=args.chaos or config.CHAOS_FAULT, replay=Path(args.replay) if args.replay else None)
        if outcome.incident_id and not args.no_agent:
            from autopilot.agents.orchestrator import handle_incident
            handle_incident(store, outcome.incident_id)
        return 0 if not outcome.failing else 1
    finally:
        store.close()


def cmd_loop(args: argparse.Namespace) -> int:
    store = Store()
    try:
        while True:
            try:
                outcome = run_once(store, chaos=args.chaos)
                if outcome.incident_id and not args.no_agent:
                    from autopilot.agents.orchestrator import handle_incident
                    handle_incident(store, outcome.incident_id)
            except KeyboardInterrupt:
                raise
            except Exception as e:  # keep the loop alive
                ui.fail(f"run error: {type(e).__name__}: {e}")
            args.chaos = None  # inject at most once per loop invocation
            ui.info(f"sleeping {args.interval}s")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0
    finally:
        store.close()


def cmd_incident(args: argparse.Namespace) -> int:
    from autopilot.agents.orchestrator import handle_incident
    store = Store()
    try:
        handle_incident(store, args.incident_id)
        return 0
    finally:
        store.close()


def cmd_incidents(args: argparse.Namespace) -> int:
    store = Store()
    try:
        rows = store.incidents()
        for r in rows:
            ui.console.print(f"{r['incident_id']}  {r['opened_at']}  {r['status']:16s}  run={r['run_id']}  checks={r['failing_checks']}")
        if not rows:
            ui.info("no incidents yet")
        return 0
    finally:
        store.close()


def cmd_faults(args: argparse.Namespace) -> int:
    for name, desc in faults.list_faults().items():
        ui.console.print(f"[yellow]{name:22s}[/yellow] {desc}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    p = config.REPORTS_DIR / f"{args.incident_id}.md"
    if not p.exists():
        ui.fail(f"no report at {p}")
        return 1
    ui.console.print(p.read_text(encoding="utf-8"))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="autopilot", description="Self-healing flight-data pipeline")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run", help="one pipeline run (fetch -> models -> checks -> agents on failure)")
    p.add_argument("--chaos", choices=sorted(faults.REGISTRY), help="inject a labelled upstream fault")
    p.add_argument("--replay", help="use a saved raw snapshot instead of calling OpenSky")
    p.add_argument("--no-agent", action="store_true", help="open the incident but do not run the agents")
    p.set_defaults(fn=cmd_run)
    p = sub.add_parser("loop", help="run continuously")
    p.add_argument("--interval", type=int, default=60)
    p.add_argument("--chaos", choices=sorted(faults.REGISTRY), help="inject once, on the first run")
    p.add_argument("--no-agent", action="store_true")
    p.set_defaults(fn=cmd_loop)
    p = sub.add_parser("incident", help="run the agents on an existing incident")
    p.add_argument("incident_id")
    p.set_defaults(fn=cmd_incident)
    sub.add_parser("incidents", help="list incidents").set_defaults(fn=cmd_incidents)
    sub.add_parser("faults", help="list chaos faults").set_defaults(fn=cmd_faults)
    p = sub.add_parser("report", help="print an incident report")
    p.add_argument("incident_id")
    p.set_defaults(fn=cmd_report)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
