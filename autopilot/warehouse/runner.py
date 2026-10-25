"""Tiny model runner: executes models/<layer>/*.sql in layer order against any DuckDB connection.
dbt-shaped layout, no dbt: the remediation agent needs to execute model SQL against arbitrary
connections (sandbox files, MotherDuck) in-process, without a subprocess or profiles.yml."""
from __future__ import annotations

from pathlib import Path

import duckdb

from autopilot import config

LAYERS = ("bronze", "silver", "gold")


def model_path(name: str) -> Path:
    for layer in LAYERS:
        p = config.MODELS_DIR / layer / f"{name}.sql"
        if p.exists():
            return p
    raise FileNotFoundError(f"unknown model: {name}")


def list_models(layers: tuple[str, ...] = LAYERS) -> list[str]:
    names: list[str] = []
    for layer in layers:
        names += [p.stem for p in sorted((config.MODELS_DIR / layer).glob("*.sql"))]
    return names


def read_model_sql(name: str) -> str:
    return model_path(name).read_text(encoding="utf-8")


def write_model_sql(name: str, sql: str) -> Path:
    p = model_path(name)
    p.write_text(sql.rstrip() + "\n", encoding="utf-8")
    return p


def run_model(conn: duckdb.DuckDBPyConnection, name: str, sql: str | None = None) -> int | None:
    """Execute one model. Returns the resulting table's row count when the model created a table."""
    conn.execute(sql if sql is not None else read_model_sql(name))
    try:
        return conn.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
    except duckdb.Error:
        return None


def run_models(conn: duckdb.DuckDBPyConnection, layers: tuple[str, ...] = LAYERS,
               overrides: dict[str, str] | None = None) -> dict[str, int | None]:
    """Run every model in the given layers in order. `overrides` maps model name -> SQL to run
    instead of the file on disk (used to test a proposed fix in the sandbox)."""
    overrides = overrides or {}
    results: dict[str, int | None] = {}
    for name in list_models(layers):
        results[name] = run_model(conn, name, overrides.get(name))
    return results
