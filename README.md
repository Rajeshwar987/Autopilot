# Autopilot — a self-healing flight-data pipeline

Live aircraft positions from [OpenSky Network](https://opensky-network.org) land in DuckDB every minute.
When a data-quality check fails, two LLM agents take over: one **diagnoses** the root cause with
read-only tools, the other **rewrites the SQL model**, proves the fix in a **sandbox copy** of the
warehouse, and asks a **human in the terminal** before anything touches prod. Every incident ends with
a Markdown post-mortem the agents did not get to edit.

```
OpenSky /states/all ──► ingest ──► chaos (optional, labelled) ──► data/raw/*.json
                                                                      │
   bronze_states   raw vector kept as LIST(VARCHAR) — never fails to load
        ▼  models/silver/silver_flights.sql   TRY_CAST(state[i]) …  ← the file the agents edit
   silver_flights ──► gold_snapshot_metrics, gold_country_counts
        ▼
   quality/checks.yml (15 checks, each one SQL statement) + per-position drift profile
        ▼ fail
   incident ──► sandbox/<INC>.duckdb (last hour copied from prod)
        ▼
   Diagnosis agent (read-only tools) ──Diagnosis──► Remediation agent
        propose_fix → apply_to_sandbox → run_checks_on_sandbox → request_human_approval
        → apply_to_prod (only with a recorded approval) → write_incident_report
```

## Why this exists

Most "AI + data pipeline" demos bolt a chatbot onto a table. This one puts the agent where the
on-call engineer actually is: a check is red at 3 a.m., something upstream changed shape, and someone
has to work out *what* and ship a safe fix. The interesting engineering is not the prompt — it is the
sandbox, the approval gate, the SQL allow-list, and the fact that prod is written by exactly one
function that refuses to run without a human's `y`.

## Honesty note: the chaos module

OpenSky's schema is stable, so real breakage does not happen on demand. `autopilot/chaos/faults.py`
injects **labelled** faults into a live snapshot — each one a class of change real upstreams produce:

| fault | what it simulates | caught by |
|---|---|---|
| `velocity_knots` | unit change, no schema change (values ×1.94) | `velocity_in_range` |
| `drop_field_sensors` | a positional field removed → everything after it shifts | `category_present`, `squawk_format`, `position_source_accepted` |
| `extra_field_inserted` | a field added mid-record → everything after it shifts | `longitude_in_range`, `on_ground_is_boolean`, … |
| `timestamp_ms` | epoch seconds become milliseconds | `freshness`, `last_contact_plausible_epoch` |
| `null_flood_position` | receiver outage: 70 % of positions null | `position_null_rate` |
| `duplicate_icao24` | at-least-once delivery | `unique_icao24_per_snapshot` |
| `stale_snapshot` | a stuck feed replays the previous snapshot | `freshness` (**not fixable** — the agent should say so) |

The CLI prints a red `[CHAOS]` banner, the label is stored in `ops.duckdb` in a column **no agent tool
can read**, and every incident report discloses it in the footer. The agents diagnose from the data alone.

## Quick start

```powershell
py -3.14 -m venv .venv ; .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env          # set LLM_API_KEY (+ optional OpenSky / MotherDuck credentials)

py -m autopilot run                              # fetch → models → checks (anonymous OpenSky works)
py -m autopilot run --chaos velocity_knots       # inject a fault → incident → agents → approval prompt
py -m autopilot run --replay data/raw/states_<t>.json --chaos drop_field_sensors   # offline, reproducible
py -m autopilot loop --interval 60               # keep ingesting
py -m autopilot incidents ; py -m autopilot report INC-0001
py -m autopilot faults                           # list chaos faults
py -m autopilot reset --yes                      # drop warehouse tables + incident store for a clean take
python -m pytest -q                              # 37 tests: guardrails, every fault is caught, check engine
```

Set `WAREHOUSE_URL=md:autopilot?motherduck_token=…` to run prod on [MotherDuck](https://motherduck.com);
the same SQL runs unchanged against the local file or the cloud, and the database is created on first
connect. DuckDB is pinned to 1.5.6 because MotherDuck's extension does not support 2.0.0 yet (Oct 2026).
Bronze loads use batched multi-row INSERTs - one round trip per 1,000 aircraft, not one per row.

## Runs on any model

The agents talk to any OpenAI-compatible chat-completions endpoint through a ~150-line loop
(`autopilot/agents/llm.py`): `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`. It was built on Alibaba Model
Studio's token plan with `qwen3.7-plus`; point it at OpenAI, OpenRouter, Groq, vLLM or Ollama by
changing three environment variables. Token usage per agent run is recorded in `ops.duckdb` and
printed in every incident report.

## Guardrails (the part worth copying)

1. **Tool lists are the permission model.** The diagnosis agent's tool list contains no write operation.
2. **Sandbox first.** Agents only ever hold a connection to `sandbox/<INC>.duckdb`, opened with
   `enable_external_access = false` (no file reads, no `ATTACH`, no extensions) and, for diagnosis, `read_only=True`.
3. **SQL allow-list** (`agents/sqlguard.py`): one statement per call; read-only tools accept
   `SELECT/WITH/DESCRIBE/SUMMARIZE`; a fix must be `CREATE OR REPLACE TABLE <silver model> AS …`;
   backfills are `UPDATE/DELETE … WHERE`, `INSERT`, `ALTER … ADD COLUMN`; results capped at 50 rows / 4 KB / 10 s.
4. **Fixes must be conditional** (branch on `len(state)`, magnitude, or `snapshot_time`) and are
   re-checked against the whole hour of history in the sandbox, so a fix that breaks old rows fails its own tests.
5. **Prod is written by one function**, `apply_to_prod`, which refuses without an `approved` row in the
   store and passing sandbox checks, backs up the model file, runs in a transaction and restores the file on error.
6. **Caps:** 25 tool calls and 5 minutes per agent run; at most 3 proposals per incident.

## Design decisions

- **No dbt.** The remediation agent needs to execute model SQL against arbitrary connections (sandbox
  files, MotherDuck) in-process. A 40-line runner over a dbt-shaped `models/` tree does that without a
  subprocess, a `profiles.yml` per sandbox, or 5-10 s of startup per iteration.
- **No Great Expectations / soda.** `quality/engine.py` compiles YAML checks to single SQL statements
  (~120 lines). The agent sees the exact SQL that failed, which is what makes the diagnosis grounded.
- **Bronze is untyped on purpose.** The raw vector is stored as `LIST(VARCHAR)` so a shifted or re-unit'd
  field still loads and is caught in silver, where SQL can fix it.
- **Generic drift detection.** `quality/profile.py` profiles every list position (type, null %, median,
  field count) for the failing run vs the last good run. That is how the agent spots faults nobody wrote a check for.

## Layout

```
autopilot/
  pipeline.py            one run: fetch → chaos → bronze → silver/gold → checks → incident
  ingest/opensky.py      anonymous or OAuth2 client-credentials; raw snapshots saved for replay
  chaos/faults.py        labelled fault registry
  warehouse/db.py        connect (local | md:), bronze loader, sandbox creation, connection hardening
  warehouse/runner.py    run models/<layer>/*.sql on any connection
  models/{bronze,silver,gold}/*.sql
  quality/engine.py      YAML → SQL check engine        quality/profile.py  per-position drift diff
  ops/store.py           ops.duckdb: runs, checks, incidents, agent runs, diagnoses, proposals, approvals
  agents/llm.py          provider-agnostic tool-calling loop
  agents/sqlguard.py     allow-lists, caps, timeouts
  agents/tools_diagnosis.py   agents/tools_remediation.py   agents/orchestrator.py   agents/prompts.py
  report/incident_report.py   Markdown post-mortem assembled from the store
tests/                   guardrails, every chaos fault is caught, check engine semantics
```
