# Incident INC-0002

- **Opened:** 2026-10-25 21:08:38.628307  
- **Run:** `run-20261025-210821-52f604` snapshot_time=1792962280 rows=2612 source=replay  
- **Failing checks:** freshness  
- **Status:** diagnosing

## Summary (written by the remediation agent)

The diagnosis agent concluded this incident is **not fixable with SQL** (`stale_source`): The upstream OpenSky source delivered the identical snapshot data for run-20261025-210821-52f604 as it did for the previous good run run-20261025-210440-1b926f. Both runs have snapshot_time=1792962280 and all 2612 state vectors are byte-for-byte identical. The freshness check failed because snapshot_time did not advance. This is a stale/repeated source delivery, not a data transformation error.

Recommended action: This is a source-level issue: the OpenSky API returned the same snapshot for consecutive fetches. The pipeline should deduplicate by snapshot_time or detect repeated payloads and skip re-ingestion. No SQL fix can create data that was not delivered by the source.

## Diagnosis (diagnosis agent)

- **Fault type:** `stale_source` (confidence 0.98, fixable=False)  
- **Affected:** bronze_states -> snapshot_time, state  
- **Root cause:** The upstream OpenSky source delivered the identical snapshot data for run-20261025-210821-52f604 as it did for the previous good run run-20261025-210440-1b926f. Both runs have snapshot_time=1792962280 and all 2612 state vectors are byte-for-byte identical. The freshness check failed because snapshot_time did not advance. This is a stale/repeated source delivery, not a data transformation error.

Evidence:

- snapshot_time=1792962280 for both failing run and baseline run (identical)
- All 2612 rows have identical state vectors between the two runs (confirmed by join on icao24)
- time_position range identical: min=1792956835, max=1792959768 for both runs
- last_contact range identical: min=1792959453, max=1792959768 for both runs
- Freshness check message: 'snapshot_time did not advance since previous run'
- No schema diff changes detected - field_count=18 for both runs

Suggested strategy: This is a source-level issue: the OpenSky API returned the same snapshot for consecutive fetches. The pipeline should deduplicate by snapshot_time or detect repeated payloads and skip re-ingestion. No SQL fix can create data that was not delivered by the source.

## Checks on the failing run (before)

| check | status | failed/total | detail |
|---|---|---:|---|
| freshness | failed | 0/2612 | snapshot_time did not advance since previous run |
| airborne_have_velocity | passed | 0/2612 |  |
| baro_altitude_in_range | passed | 0/2612 | min=-38.1 median=10294.619999999999 max=13716.0 |
| category_present | passed | 0/2612 |  |
| icao24_not_null | passed | 0/2612 |  |
| last_contact_plausible_epoch | passed | 0/2612 | min=1792959453 median=1792959768.0 max=1792959768 |
| latitude_in_range | passed | 0/2612 | min=35.0406 median=46.7184 max=69.4188 |
| longitude_in_range | passed | 0/2612 | min=-14.2488 median=8.78585 max=39.8282 |
| on_ground_is_boolean | passed | 0/2612 |  |
| position_null_rate | passed | 0/2612 |  |
| position_source_accepted | passed | 0/2612 | distinct=1 |
| row_count_min | passed | 0/2612 | min_expected=200 |
| squawk_format | passed | 0/2612 |  |
| unique_icao24_per_snapshot | passed | 0/2612 | duplicated_keys=0 |
| velocity_in_range | passed | 13/2612 | min=0.0 median=214.13 max=1748.67 |

## Agent runs

| agent | model | tool calls | tokens in/out | status |
|---|---|---:|---:|---|
| diagnosis | qwen3.7-plus | 9 | 24356/3348 | terminal_tool |

Total tokens: 24356 in / 3348 out.

---

**Disclosure:** this incident was produced by Autopilot's chaos module (`stale_snapshot`), which injects a realistic upstream fault into a live OpenSky snapshot. The agents had no access to this label; they diagnosed and fixed it from the data alone.
