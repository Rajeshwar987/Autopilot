"""Fetch live state vectors from OpenSky and persist raw snapshots.

Anonymous access works (400 credits/day, 10 s resolution). With OPENSKY_CLIENT_ID/SECRET we use
OAuth2 client-credentials (4,000 credits/day). A Europe-sized bbox costs 4 credits per call.
Every response is saved verbatim under data/raw/ so demos can be replayed offline."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from autopilot import config

_token: dict = {"value": None, "expires_at": 0.0}


class OpenSkyError(RuntimeError):
    pass


def _get_token() -> str | None:
    if not (config.OPENSKY_CLIENT_ID and config.OPENSKY_CLIENT_SECRET):
        return None
    if _token["value"] and time.time() < _token["expires_at"] - 30:
        return _token["value"]
    resp = httpx.post(
        config.OPENSKY_TOKEN_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": config.OPENSKY_CLIENT_ID,
            "client_secret": config.OPENSKY_CLIENT_SECRET,
        },
        timeout=20,
    )
    resp.raise_for_status()
    body = resp.json()
    _token["value"] = body["access_token"]
    _token["expires_at"] = time.time() + float(body.get("expires_in", 1800))
    return _token["value"]


def fetch_states(bbox: tuple[float, float, float, float] | None = None, retries: int = 3) -> dict:
    """GET /states/all for the bbox. Returns {"time": int, "states": [[...18 fields...], ...]}.
    Backs off on 429/5xx; raises OpenSkyError after `retries` attempts."""
    lamin, lomin, lamax, lomax = bbox or config.opensky_bbox()
    params = {"lamin": lamin, "lomin": lomin, "lamax": lamax, "lomax": lomax, "extended": 1}
    headers = {}
    token = _get_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    delay = 5.0
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            resp = httpx.get(f"{config.OPENSKY_API}/states/all", params=params, headers=headers, timeout=30)
            if resp.status_code in (429, 500, 502, 503, 504):
                raise OpenSkyError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            resp.raise_for_status()
            payload = resp.json()
            if not isinstance(payload, dict) or "time" not in payload:
                raise OpenSkyError(f"unexpected payload: {str(payload)[:200]}")
            payload["states"] = payload.get("states") or []
            return payload
        except (OpenSkyError, httpx.HTTPError) as e:  # noqa: PERF203
            last_err = e
            if attempt < retries - 1:
                time.sleep(delay)
                delay *= 2
    raise OpenSkyError(f"OpenSky fetch failed after {retries} attempts: {last_err}")


@dataclass
class RawSnapshot:
    path: Path
    payload: dict
    chaos_fault: str | None = None
    source: str = "opensky"  # opensky | replay | fallback


def save_raw(payload: dict, chaos_fault: str | None = None, source: str = "opensky") -> RawSnapshot:
    """Write the payload verbatim to data/raw/states_<time>.json plus a .meta.json sidecar.
    The sidecar carries the chaos label; agent tools never read raw files or sidecars."""
    ts = int(payload.get("time") or time.time())
    path = config.DATA_RAW / f"states_{ts}.json"
    # avoid clobbering when chaos runs produce the same snapshot time
    i = 1
    while path.exists():
        path = config.DATA_RAW / f"states_{ts}_{i}.json"
        i += 1
    path.write_text(json.dumps(payload), encoding="utf-8")
    meta = {"fetched_at": time.time(), "chaos_fault": chaos_fault, "source": source,
            "n_states": len(payload.get("states") or [])}
    path.with_suffix(".meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return RawSnapshot(path=path, payload=payload, chaos_fault=chaos_fault, source=source)


def load_raw(path: Path | str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def latest_raw_path(exclude_chaos: bool = True) -> Path | None:
    """Most recent saved snapshot (optionally skipping chaos-injected ones)."""
    files = sorted(config.DATA_RAW.glob("states_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in files:
        if p.name.endswith(".meta.json"):
            continue
        if exclude_chaos:
            meta_p = p.with_suffix(".meta.json")
            if meta_p.exists() and json.loads(meta_p.read_text(encoding="utf-8")).get("chaos_fault"):
                continue
        return p
    return None
