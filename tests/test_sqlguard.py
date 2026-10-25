import duckdb
import pytest

from autopilot.agents import sqlguard as g


@pytest.mark.parametrize("sql", [
    "SELECT 1",
    "with x as (select 1) select * from x",
    "DESCRIBE silver_flights",
    "SUMMARIZE bronze_states",
    "-- comment\nSELECT 2;",
])
def test_readonly_accepts(sql):
    assert g.validate_readonly(sql)


@pytest.mark.parametrize("sql", [
    "DROP TABLE silver_flights",
    "SELECT 1; DROP TABLE x",
    "UPDATE silver_flights SET velocity = 0",
    "ATTACH 'x.duckdb' AS y",
    "SELECT * FROM read_csv('C:/secrets.csv')",
    "COPY silver_flights TO 'out.csv'",
    "INSTALL httpfs",
    "",
])
def test_readonly_rejects(sql):
    with pytest.raises(g.SQLGuardError):
        g.validate_readonly(sql)


def test_literal_semicolon_is_fine():
    assert g.validate_readonly("SELECT ';' AS s")


def test_model_sql_must_target_model():
    good = "CREATE OR REPLACE TABLE silver_flights AS SELECT * FROM bronze_states"
    assert g.validate_model_sql(good, "silver_flights")
    with pytest.raises(g.SQLGuardError):
        g.validate_model_sql(good, "gold_snapshot_metrics")
    with pytest.raises(g.SQLGuardError):
        g.validate_model_sql("CREATE TABLE silver_flights AS SELECT 1", "silver_flights")
    with pytest.raises(g.SQLGuardError):
        g.validate_model_sql(good + "; DROP TABLE bronze_states", "silver_flights")


def test_backfill_rules():
    assert len(g.validate_backfill("UPDATE t SET a = 1 WHERE b = 2; DELETE FROM t WHERE a IS NULL")) == 2
    with pytest.raises(g.SQLGuardError):
        g.validate_backfill("DELETE FROM t")
    with pytest.raises(g.SQLGuardError):
        g.validate_backfill("ALTER TABLE t DROP COLUMN a")
    with pytest.raises(g.SQLGuardError):
        g.validate_backfill("TRUNCATE t")


def test_run_readonly_caps_rows_and_formats():
    conn = duckdb.connect()
    out = g.run_readonly(conn, "SELECT range AS n FROM range(100)", limit=5)
    assert out.count("\n") >= 6 and "more rows" in out


def test_hardened_connection_blocks_file_access(tmp_path):
    from autopilot.warehouse import db
    conn = db.harden(duckdb.connect((tmp_path / "sb.duckdb").as_posix()))
    with pytest.raises(duckdb.Error):
        conn.execute("SELECT * FROM read_csv('nope.csv')")
    with pytest.raises(duckdb.Error):
        conn.execute("SET enable_external_access = true")
