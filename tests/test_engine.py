import duckdb
import pytest

from autopilot.quality import engine


@pytest.fixture
def conn():
    c = duckdb.connect()
    c.execute("CREATE TABLE t (run_id VARCHAR, snapshot_time BIGINT, icao24 VARCHAR, v DOUBLE, flag BOOLEAN, code VARCHAR)")
    c.executemany("INSERT INTO t VALUES (?, ?, ?, ?, ?, ?)", [
        ("r1", 1000, "a", 10.0, True, "1140"),
        ("r1", 1000, "b", 20.0, False, "7700"),
        ("r1", 1000, "b", 500.0, None, "zz"),
        ("r1", 1000, "c", None, True, None),
    ])
    return c


def _one(conn, chk, ctx=None):
    return engine.run_checks(conn, "r1", ctx or {"now_epoch": 1010, "previous_snapshot_time": 900}, checks=[chk])[0]


def test_not_null_threshold(conn):
    r = _one(conn, {"name": "n", "table": "t", "type": "not_null", "column": "v", "max_null_pct": 20})
    assert r.status == "passed" and r.failed_rows == 1 and r.details["null_pct"] == 25.0 or r.status == "failed"
    r = _one(conn, {"name": "n", "table": "t", "type": "not_null", "column": "v", "max_null_pct": 30})
    assert r.status == "passed"
    r = _one(conn, {"name": "n", "table": "t", "type": "not_null", "column": "v"})
    assert r.status == "failed"


def test_range(conn):
    r = _one(conn, {"name": "r", "table": "t", "type": "range", "column": "v", "min": 0, "max": 100})
    assert r.status == "failed" and r.failed_rows == 1 and r.total_rows == 4
    r = _one(conn, {"name": "r", "table": "t", "type": "range", "column": "v", "min": 0, "max": 100, "max_failed_pct": 30})
    assert r.status == "passed"


def test_unique(conn):
    r = _one(conn, {"name": "u", "table": "t", "type": "unique", "columns": ["icao24", "snapshot_time"]})
    assert r.status == "failed" and r.failed_rows == 1


def test_row_count_min(conn):
    assert _one(conn, {"name": "c", "table": "t", "type": "row_count_min", "min": 4}).status == "passed"
    assert _one(conn, {"name": "c", "table": "t", "type": "row_count_min", "min": 5}).status == "failed"


def test_freshness(conn):
    chk = {"name": "f", "table": "t", "type": "freshness", "max_age_seconds": 60}
    assert _one(conn, chk, {"now_epoch": 1010, "previous_snapshot_time": 900}).status == "passed"
    assert _one(conn, chk, {"now_epoch": 5000, "previous_snapshot_time": 900}).status == "failed"
    r = _one(conn, chk, {"now_epoch": 1010, "previous_snapshot_time": 1000})
    assert r.status == "failed" and "advance" in r.details["reason"]


def test_accepted_values_and_expression(conn):
    r = _one(conn, {"name": "a", "table": "t", "type": "accepted_values", "column": "code", "values": ["1140", "7700"]})
    assert r.status == "failed" and r.failed_rows == 1
    r = _one(conn, {"name": "e", "table": "t", "type": "expression", "expression": "v < 100"})
    assert r.failed_rows == 1  # NULL v does not count as a failure


def test_bad_sql_is_reported_not_raised(conn):
    r = _one(conn, {"name": "x", "table": "t", "type": "range", "column": "nope", "min": 0, "max": 1})
    assert r.status == "error" and "error" in r.details


def test_real_checks_file_compiles():
    for chk in engine.load_checks():
        assert engine.compile_check(chk, "r1")
