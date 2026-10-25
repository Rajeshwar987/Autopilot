# LinkedIn: post drafts + recording script

Numbers below are from the real run on 2026-10-25 (`reports/samples/velocity_knots.md`):
diagnosis agent 8 tool calls / 61 s / 24.5k+3.3k tokens; remediation agent 10 tool calls / 133 s / 40.4k+7.2k tokens;
2,612 aircraft over Europe; mean airborne velocity 406.4 → 209.1 m/s; 1,803 rows out of range → 0.

---

## Post 1 — the launch (video post)

**Hook (first two lines are all anyone sees before "...more"):**

> I gave two AI agents on-call duty for a data pipeline, then broke the pipeline on purpose.
> Here is what happened in the 3 minutes after the alert.

**Body:**

Autopilot ingests live aircraft positions from OpenSky every minute into DuckDB — bronze → silver → gold, 15 data-quality checks, nothing exotic.

Then I changed the upstream feed so ground speed arrived in knots instead of m/s. No schema change. Every value just ~1.94× too big. The kind of bug that silently corrupts a dashboard for a week.

What the agents did, with no hint about what I had broken:

🔎 Diagnosis agent (read-only tools): pulled the failing check and the per-position drift profile, then joined the same aircraft across the last good snapshot and the bad one. Ratio: 1.9438 on every single row. "unit_change: velocity now in knots, confidence 0.92." 8 tool calls, 61 seconds.

🔧 Remediation agent: rewrote one SQL model with a conditional conversion, ran it in a **sandbox copy** of the warehouse, re-ran all 15 checks on the bad run and 13 on the full hour of history. Mean velocity 406 → 209 m/s. Then it stopped and asked me.

👤 Me: `y`.

The fix landed in prod, the model file in git changed, and a post-mortem was written — with a footer that says the fault was injected by my chaos module, because the agents never got to read that label.

The interesting engineering is not the prompt. It is:
• the diagnosis agent's tool list contains no write operation — the tool list *is* the permission model
• agents only ever hold a connection to a sandbox file with external access disabled
• a fix must be `CREATE OR REPLACE TABLE <model> AS …`, validated by an allow-list, and must survive the historical rows too
• prod is written by exactly one function that refuses to run without a recorded human approval

Stack: Python, DuckDB (MotherDuck for prod), hand-written SQL models, a 120-line YAML check engine, and a provider-agnostic agent loop — runs on any OpenAI-compatible model; I used Qwen 3.7 Plus. Whole incident cost about 75k tokens.

Repo in the comments. Fault injection is labelled everywhere — if a demo of "self-healing" doesn't tell you how it broke the thing, ask.

#DataEngineering #AIAgents #DuckDB #DataQuality #AgenticAI

---

## Post 2 — the positional shift (carousel or screenshots, 2-3 days later)

> Five alarms. One root cause. Zero hints.

Dropped one field from the middle of an 18-field positional record. Downstream, latitudes landed in altitude columns, booleans became integers, the last field went 100% NULL. Three checks fired at once.

The drift profiler showed field_count 18 → 17 and type flips at positions 15 and 16. The agent read that as a shift, not three unrelated bugs, and proposed a `CASE WHEN len(state) = 17 THEN state[13] ELSE state[14] END` remap so the 18-field history stayed untouched. Sandbox: 15/15 on the bad run, 13/13 on history.

(Screens: the schema-diff tool output; the unified diff; the approval prompt.)

---

## Post 3 — the one where the agent says no (text post)

> The most important thing my remediation agent did this week was refuse to work.

Replayed a stale snapshot — the upstream feed stuck, same data served twice. Freshness check fails. The diagnosis agent looked at it and recorded `fixable=false`: "the data is repeated, not malformed; SQL cannot create data that wasn't delivered. Alert the upstream owner."

No proposal. No approval prompt. Report written, incident closed as `no_fix`.

If your agent demo never shows it declining, you haven't shown me it's safe.

---

## Comment to post under Post 1

Repo: https://github.com/Rajeshwar987/Autopilot. Honest version of the pitch: OpenSky's schema is stable, so I built a chaos module that injects seven kinds of realistic upstream faults (unit change, dropped field, inserted field, ms timestamps, null flood, duplicates, stale feed). The CLI prints a red CHAOS banner, the label is stored in a column no agent tool can read, and every report discloses it.

---

## 75-second recording script (PowerShell, dark theme, font 18+, window ~110 cols)

Prep: `py -m autopilot reset --yes` (clean warehouse + incident store; restore the model with `git checkout -- autopilot/models/silver` if a previous take applied a fix), then `py -m autopilot run --replay data/raw/states_<t>.json --no-agent` once so there is a baseline; clear the screen.

| t | action | what the viewer sees |
|---|---|---|
| 0-8 s | `py -m autopilot run --replay data/raw/states_<t>.json` | "replaying … 2612 aircraft", gold line "mean airborne velocity=209.1 m/s", table "15/15 passed", `OK all checks passed` |
| 8-16 s | `py -m autopilot run --replay … --chaos velocity_knots` | red CHAOS panel; gold line now ~406 m/s; `FAILED velocity_in_range: 1803/2612`; `!! incident INC-0002 opened` |
| 16-40 s | (wait) | Diagnosis agent panel; tool calls stream: `get_check_results` → `get_schema_diff` → `sample_rows` → `run_sql_readonly` (the icao24 self-join) → `record_diagnosis`; Diagnosis table with ratio 1.9438 |
| 40-62 s | (wait) | Remediation panel; `propose_fix` shows the yellow diff; `apply_to_sandbox`; `run_checks_on_sandbox` "15/15 … BEFORE 406.4 … AFTER 209.1" |
| 62-70 s | type `y` | HUMAN APPROVAL REQUIRED panel, diff, sandbox results, prompt → `OK applied FIX-0001 to prod: 15/15` |
| 70-75 s | `py -m autopilot report INC-0002` | report scrolls; end on the Disclosure footer |

Tips: record 3 takes; the agents are not deterministic. Use `--replay` so no take depends on the network.
Trim dead air during the agent waits to ~10 s each; keep the real timings in the caption.
