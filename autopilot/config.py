"""Central configuration: paths and environment. All paths are absolute pathlib.Path objects;
pass `.as_posix()` into DuckDB so Windows backslashes never reach SQL."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

PACKAGE_DIR = ROOT / "autopilot"
MODELS_DIR = PACKAGE_DIR / "models"
CHECKS_FILE = PACKAGE_DIR / "quality" / "checks.yml"
DATA_RAW = ROOT / "data" / "raw"
SANDBOX_DIR = ROOT / "sandbox"
REPORTS_DIR = ROOT / "reports"
OPS_DB = ROOT / "ops.duckdb"

for _d in (DATA_RAW, SANDBOX_DIR, REPORTS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ---- LLM (any OpenAI-compatible endpoint) ----
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://token-plan.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "qwen3.7-plus")

# ---- Warehouse ----
def warehouse_url() -> str:
    """'md:...' is passed through; anything else is a local file resolved under ROOT."""
    url = os.getenv("WAREHOUSE_URL", "autopilot.duckdb")
    if url.startswith("md:"):
        return url
    return (ROOT / url).resolve().as_posix()


# ---- OpenSky ----
OPENSKY_CLIENT_ID = os.getenv("OPENSKY_CLIENT_ID", "")
OPENSKY_CLIENT_SECRET = os.getenv("OPENSKY_CLIENT_SECRET", "")
OPENSKY_API = "https://opensky-network.org/api"
OPENSKY_TOKEN_URL = (
    "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
)


def opensky_bbox() -> tuple[float, float, float, float]:
    """(lamin, lomin, lamax, lomax). Default is Europe."""
    raw = os.getenv("OPENSKY_BBOX", "35,-15,72,40")
    lamin, lomin, lamax, lomax = (float(x) for x in raw.split(","))
    return lamin, lomin, lamax, lomax


# ---- Chaos ----
CHAOS_FAULT = os.getenv("CHAOS_FAULT", "") or None

# OpenSky /states/all field order (0-based API index). Shared by the chaos module and the
# diagnosis prompt; the silver model repeats it in SQL comments.
STATE_FIELDS = [
    "icao24", "callsign", "origin_country", "time_position", "last_contact",
    "longitude", "latitude", "baro_altitude", "on_ground", "velocity",
    "true_track", "vertical_rate", "sensors", "geo_altitude", "squawk",
    "spi", "position_source", "category",
]
