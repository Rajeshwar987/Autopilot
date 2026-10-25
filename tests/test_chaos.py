"""Every chaos fault must be caught by at least one check, and a clean snapshot must pass them all."""
import copy
import json
import time

import duckdb
import pytest

from autopilot.chaos import faults
from autopilot.quality import engine, profile
from autopilot.warehouse import db, runner

NOW = 1_792_959_768


def make_payload(n: int = 300) -> dict:
    states = []
    for i in range(n):
        states.append([
            f"{i:06x}", f"TST{i:04d} ", "Germany", NOW - 1, NOW - 1,
            8.0 + (i % 50) * 0.3, 48.0 + (i % 30) * 0.5, 9000.0 + i, False,
            200.0 + (i % 60), 90.0, 0.5, None, 9300.0 + i, "1140", False, 0, i % 20,
        ])
    # a few on the ground with no position, like the real feed
    for s in states[:30]:
        s[8] = True; s[5] = None; s[6] = None; s[9] = 3.0
    return {"time": NOW, "states": states}


@pytest.fixture
def conn():
    c = duckdb.connect()
    runner.run_models(c, ("bronze",))
    base = make_payload(); base["time"] = NOW - 120
    db.load_bronze(c, "run-base", base)
    return c


def _run(c, run_id, payload, prev=NOW - 120):
    db.load_bronze(c, run_id, payload)
    runner.run_models(c, ("silver",))
    return engine.summarize(engine.run_checks(c, run_id, {"now_epoch": NOW + 30, "previous_snapshot_time": prev}))


def test_clean_snapshot_passes(conn):
    s = _run(conn, "run-clean", make_payload())
    assert s["failed"] == [], s


@pytest.mark.parametrize("name", sorted(faults.REGISTRY))
def test_every_fault_is_caught(conn, name):
    clean = make_payload()
    ctx = faults.ChaosContext(previous_loader=lambda: {"time": NOW - 120, "states": copy.deepcopy(clean["states"])})
    p = faults.apply_fault(name, clean, ctx)
    s = _run(conn, f"run-{name}", p)
    assert s["failed"], f"{name} was not caught by any check"


def test_apply_fault_does_not_mutate_input():
    p = make_payload()
    before = json.dumps(p)
    faults.apply_fault("drop_field_sensors", p)
    assert json.dumps(p) == before


def test_schema_diff_detects_shift(conn):
    p = faults.apply_fault("drop_field_sensors", make_payload())
    db.load_bronze(conn, "run-shift", p)
    d = profile.schema_diff(conn, "run-shift", "run-base")
    kinds = {c["kind"] for c in d["changes"]}
    assert "field_count" in kinds and d["field_count"]["current"]["mode"] == 17


def test_schema_diff_detects_unit_change(conn):
    p = faults.apply_fault("velocity_knots", make_payload())
    db.load_bronze(conn, "run-knots", p)
    d = profile.schema_diff(conn, "run-knots", "run-base")
    vel = [c for c in d["changes"] if c["position"] == 10]
    assert vel and abs(vel[0]["ratio"] - 1.944) < 0.01
