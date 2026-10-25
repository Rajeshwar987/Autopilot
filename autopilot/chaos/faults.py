"""Chaos module: deterministic, labelled fault injection between the raw fetch and bronze.

OpenSky's schema is stable, so real breakage will not happen on demand. Each fault here simulates a
class of change a real upstream has produced (see the `Simulates:` line in each docstring). The CLI
prints a loud [CHAOS] banner and the label is stored in ingest_runs.chaos_fault - a column the
agents' tools never read. Incident reports disclose it in the footer."""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Callable

KNOTS_PER_MS = 1.0 / 0.514444  # 1 m/s = 1.94384 kn

# OpenSky indices (0-based) used below
I_TIME_POSITION, I_LAST_CONTACT, I_LON, I_LAT, I_VELOCITY, I_SENSORS = 3, 4, 5, 6, 9, 12


@dataclass
class ChaosContext:
    """Lazy access to the previous good snapshot (needed by stale_snapshot)."""
    previous_loader: Callable[[], dict | None] | None = None
    _cache: dict | None = field(default=None, repr=False)

    def previous_payload(self) -> dict | None:
        if self._cache is None and self.previous_loader is not None:
            self._cache = self.previous_loader()
        return self._cache


FaultFn = Callable[[dict, ChaosContext], dict]
REGISTRY: dict[str, FaultFn] = {}


def fault(name: str):
    def deco(fn: FaultFn) -> FaultFn:
        REGISTRY[name] = fn
        return fn
    return deco


def _scale(v, factor):
    return None if v is None else v * factor


@fault("velocity_knots")
def velocity_knots(payload: dict, ctx: ChaosContext) -> dict:
    """Velocity reported in knots instead of m/s (values ~1.94x larger, same type).
    Simulates: an upstream unit change with no schema change - the most common silent data bug."""
    for s in payload["states"]:
        s[I_VELOCITY] = _scale(s[I_VELOCITY], KNOTS_PER_MS)
    return payload


@fault("drop_field_sensors")
def drop_field_sensors(payload: dict, ctx: ChaosContext) -> dict:
    """Field 12 (`sensors`) removed, so every later field shifts one position left: geo_altitude lands
    in `sensors`, squawk in `geo_altitude`, ... and the vector has 17 elements instead of 18.
    Simulates: a positional API dropping a deprecated field (OpenSky itself appended `category` in 2022)."""
    for s in payload["states"]:
        del s[I_SENSORS]
    return payload


@fault("extra_field_inserted")
def extra_field_inserted(payload: dict, ctx: ChaosContext) -> dict:
    """A new field inserted after origin_country; every later field shifts one position right (19 elements).
    Simulates: an upstream adding a column in the middle of a positional record."""
    for s in payload["states"]:
        s.insert(3, None)
    return payload


@fault("timestamp_ms")
def timestamp_ms(payload: dict, ctx: ChaosContext) -> dict:
    """time_position and last_contact (and the snapshot `time`) in milliseconds instead of seconds.
    Simulates: an upstream switching epoch units - freshness checks see dates thousands of years out."""
    payload["time"] = int(payload["time"]) * 1000
    for s in payload["states"]:
        s[I_TIME_POSITION] = _scale(s[I_TIME_POSITION], 1000)
        s[I_LAST_CONTACT] = _scale(s[I_LAST_CONTACT], 1000)
    return payload


@fault("null_flood_position")
def null_flood_position(payload: dict, ctx: ChaosContext) -> dict:
    """~70% of aircraft lose longitude/latitude (null). Baseline null rate is ~10-15%.
    Simulates: a receiver-network outage or a broken join upstream."""
    for i, s in enumerate(payload["states"]):
        if i % 10 < 7:
            s[I_LON] = None
            s[I_LAT] = None
    return payload


@fault("duplicate_icao24")
def duplicate_icao24(payload: dict, ctx: ChaosContext) -> dict:
    """Every 10th aircraft appears twice in the same snapshot.
    Simulates: an at-least-once upstream retry or a de-dup step that stopped running."""
    dupes = [copy.deepcopy(s) for i, s in enumerate(payload["states"]) if i % 10 == 0]
    payload["states"].extend(dupes)
    return payload


@fault("stale_snapshot")
def stale_snapshot(payload: dict, ctx: ChaosContext) -> dict:
    """The previous snapshot is served again - `time` does not advance. Not fixable with SQL; the
    correct response is to alert, not to transform data.
    Simulates: a cached/stuck upstream feed."""
    prev = ctx.previous_payload()
    if prev is None:
        return payload  # nothing to replay yet; the run proceeds clean
    return copy.deepcopy(prev)


def list_faults() -> dict[str, str]:
    return {name: (fn.__doc__ or "").strip().splitlines()[0] for name, fn in REGISTRY.items()}


def describe(name: str) -> str:
    return (REGISTRY[name].__doc__ or "").strip()


def apply_fault(name: str, payload: dict, ctx: ChaosContext | None = None) -> dict:
    if name not in REGISTRY:
        raise KeyError(f"unknown chaos fault '{name}'. Known: {', '.join(REGISTRY)}")
    return REGISTRY[name](copy.deepcopy(payload), ctx or ChaosContext())
