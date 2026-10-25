"""Warehouse connections and sandbox creation. Same SQL runs on a local DuckDB file or MotherDuck."""
from __future__ import annotations

import json
from pathlib import Path

import duckdb

from autopilot import config


def connect(target: str | Path | None = None, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """target: None -> prod (WAREHOUSE_URL); 'md:...' -> MotherDuck; otherwise a local file path."""
    if target is None:
        target = config.warehouse_url()
    if isinstance(target, str) and target.startswith("md:"):
        try:
            return duckdb.connect(target, read_only=read_only)
        except duckdb.Error as e:
            if "no database/share named" not in str(e):
                raise
            # md:<name>?... -> create the database once, then reconnect
            name, _, params = target[3:].partition("?")
            root = duckdb.connect("md:" + ("?" + params if params else ""))
            root.execute(f'CREATE DATABASE IF NOT EXISTS "{name}"')
            root.close()
            return duckdb.connect(target, read_only=read_only)
    return duckdb.connect(Path(target).as_posix(), read_only=read_only)


def is_motherduck(url: str | None = None) -> bool:
    return (url or config.warehouse_url()).startswith("md:")


def load_bronze(conn: duckdb.DuckDBPyConnection, run_id: str, payload: dict) -> int:
    """Insert one raw snapshot into bronze_states, stringifying every element (NULL preserved).
    Never fails on shape changes: 16, 17 or 19 fields all load."""
    snapshot_time = int(payload["time"])

    def lit(v) -> str:  # SQL literal for one list element
        if v is None:
            return "NULL"
        s = v if isinstance(v, str) else json.dumps(v)
        return "'" + s.replace("'", "''") + "'"

    states = payload.get("states") or []
    prefix = f"INSERT INTO bronze_states (run_id, snapshot_time, ingested_at, state) VALUES "
    # multi-row VALUES in batches: one round trip per 1000 aircraft instead of one per row
    # (executemany = ~7 minutes per snapshot against MotherDuck)
    for i in range(0, len(states), 1000):
        values = ",".join(
            f"('{run_id}', {snapshot_time}, now(), [{','.join(lit(v) for v in state)}]::VARCHAR[])"
            for state in states[i:i + 1000]
        )
        conn.execute(prefix + values)
    return len(states)


def sandbox_path(incident_id: str) -> Path:
    return config.SANDBOX_DIR / f"{incident_id}.duckdb"


def create_sandbox(incident_id: str, window_seconds: int = 3600) -> Path:
    """Copy the last `window_seconds` of bronze_states and silver_flights from prod into a fresh
    sandbox file. Done from the *prod* connection by attaching the sandbox file, which works for
    both local-file prod and MotherDuck (a token cannot be given to ATTACH 'md:' directly)."""
    sb_path = sandbox_path(incident_id)
    for p in (sb_path, Path(str(sb_path) + ".wal")):
        if p.exists():
            p.unlink()
    prod = connect()
    try:
        prod.execute(f"ATTACH '{sb_path.as_posix()}' AS sb")
        window = int(window_seconds)
        prod.execute(
            "CREATE TABLE sb.bronze_states AS SELECT * FROM bronze_states "
            f"WHERE snapshot_time >= (SELECT max(snapshot_time) FROM bronze_states) - {window}"
        )
        prod.execute(
            "CREATE TABLE sb.silver_flights AS SELECT * FROM silver_flights "
            f"WHERE snapshot_time >= (SELECT max(snapshot_time) FROM silver_flights) - {window}"
        )
        prod.execute("DETACH sb")
    finally:
        prod.close()
    return sb_path


def harden(conn: duckdb.DuckDBPyConnection) -> duckdb.DuckDBPyConnection:
    """Lock a connection handed to an agent: no file access, no ATTACH, no extensions."""
    conn.execute("SET enable_external_access = false")
    conn.execute("SET lock_configuration = true")
    return conn
