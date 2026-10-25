# Incident INC-0001

- **Opened:** 2026-10-25 21:05:11.626860  
- **Run:** `run-20261025-210459-bbce2f` snapshot_time=1792962290 rows=2612 source=replay  
- **Failing checks:** position_source_accepted, category_present, squawk_format  
- **Status:** applied

## Summary (written by the remediation agent)

### Incident INC-0001 — Positional Shift in OpenSky State Vectors

#### What broke
Three quality checks failed on run `run-20261025-210459-bbce2f`:
- **position_source_accepted** — 41/2612 rows had invalid values (0-20 instead of 0-3)
- **category_present** — 2612/2612 rows had NULL category
- **squawk_format** — 2612/2612 rows contained `'false'` instead of valid squawk codes

#### Root cause
The upstream OpenSky source dropped the `sensors` field (API position 12 / list index 13), reducing the state vector length from 18 to 17 fields. Because the `silver_flights` model used hardcoded positional indexing (`state[14]` for geo_altitude, `state[15]` for squawk, etc.), all fields from `geo_altitude` onward were read from the wrong positions:

| Column | Expected (18-field) | Actually read from 17-field vector |
|---|---|---|
| geo_altitude | state[14] | state[13] ← correct by coincidence (sensors was NULL) |
| squawk | state[15] | state[14] = squawk value → but model read state[15] = `'false'` (spi) |
| spi | state[16] | state[15] = `'false'` → but model read state[16] = position_source |
| position_source | state[17] | state[16] = position_source → but model read state[17] = category (0-20) |
| category | state[18] | state[17] = category → but model read state[18] = NULL (out of bounds) |

#### What changed
Updated `silver_flights` model to use conditional `CASE` expressions keyed on `len(state)`:
- **18-field vectors** (historical): original indexing preserved (state[14]–state[18])
- **17-field vectors** (new): shifted indexing (state[13]–state[17])

This ensures both historical and new data are unpacked correctly regardless of upstream schema changes.

#### Before / After
| Check | Before | After |
|---|---|---|
| position_source_accepted | 41/2612 failed | ✅ 0 failed |
| category_present | 2612/2612 NULL | ✅ 0 failed |
| squawk_format | 2612/2612 failed | ✅ 0 failed |
| All sandbox checks (failing run) | 0/15 passed | 15/15 passed |
| All sandbox checks (history) | — | 13/13 passed |
| Prod checks after apply | — | 15/15 passed |

#### Recommendation
Monitor future runs for further field-count changes. If the upstream schema stabilises at 17 fields, consider simplifying the model to remove the conditional logic.

## Diagnosis (diagnosis agent)

- **Fault type:** `positional_shift` (confidence 1.00, fixable=True)  
- **Affected:** silver_flights -> squawk, category, position_source, geo_altitude, spi  
- **Root cause:** Upstream source dropped the sensors field (position 13), reducing state vector length from 18 to 17 fields. This caused all subsequent fields to shift left by one position. The silver_flights model uses hardcoded positional indexing (state[14] for geo_altitude, state[15] for squawk, etc.), which is now misaligned. In the failing run: state[15] contains the spi boolean ("false") instead of squawk codes, state[18] is NULL (category missing), and state[17] contains category values (0-20) instead of position_source values (0-3).

Evidence:

- field_count dropped from 18 to 17 (baseline vs failing run)
- baseline pos 13=sensors(NULL), pos 14=geo_altitude(3444.24), pos 15=squawk('1140'), pos 16=spi(false), pos 17=position_source(0), pos 18=category(0)
- failing run pos 13=geo_altitude(3444.24), pos 14=squawk('1140'), pos 15=spi(false), pos 16=position_source(0), pos 17=category(0), pos 18=NULL
- squawk_format check: 2612/2612 rows failed - state[15] now contains 'false' (spi boolean) instead of squawk codes
- category_present check: 2612/2612 rows have NULL category - state[18] doesn't exist in 17-field vectors
- position_source_accepted check: 41/2612 rows failed - state[17] contains category values (0-20) instead of position_source (0-3)

Suggested strategy: Update silver_flights model to handle variable field counts. Either: (1) detect field_count and adjust indexing dynamically, or (2) update to use the new 17-field layout if sensors field is permanently removed upstream. A robust solution would check len(state) and use conditional logic: if len(state)=17, skip sensors and use state[13] for geo_altitude, state[14] for squawk, etc.; if len(state)=18, use current indexing.

## Checks on the failing run (before)

| check | status | failed/total | detail |
|---|---|---:|---|
| category_present | failed | 2612/2612 |  |
| position_source_accepted | failed | 41/2612 | distinct=7 |
| squawk_format | failed | 2612/2612 |  |
| airborne_have_velocity | passed | 0/2612 |  |
| baro_altitude_in_range | passed | 0/2612 | min=-38.1 median=10294.619999999999 max=13716.0 |
| freshness | passed | 0/2612 | snapshot_time=1792962290 now=1792962311 age_s=21 |
| icao24_not_null | passed | 0/2612 |  |
| last_contact_plausible_epoch | passed | 0/2612 | min=1792959453 median=1792959768.0 max=1792959768 |
| latitude_in_range | passed | 0/2612 | min=35.0406 median=46.7184 max=69.4188 |
| longitude_in_range | passed | 0/2612 | min=-14.2488 median=8.78585 max=39.8282 |
| on_ground_is_boolean | passed | 0/2612 |  |
| position_null_rate | passed | 0/2612 |  |
| row_count_min | passed | 0/2612 | min_expected=200 |
| unique_icao24_per_snapshot | passed | 0/2612 | duplicated_keys=0 |
| velocity_in_range | passed | 13/2612 | min=0.0 median=214.13 max=1748.67 |

## Proposal FIX-0001 - silver_flights (approved)

**Rationale:** Upstream source dropped the sensors field (position 13), reducing state vector length from 18 to 17 fields. This caused all subsequent fields (geo_altitude, squawk, spi, position_source, category) to shift left by one position. The fix uses conditional CASE expressions based on len(state): when len(state)=17, fields are read from positions 13-17 (shifted layout); when len(state)=18, the original positions 14-18 are used. This preserves correct handling for both historical 18-field rows and new 17-field rows.

```diff
--- current
+++ proposed
@@ -8,4 +8,9 @@
 --   12 sensors [13] (int[] or null) | 13 geo_altitude [14] (m) | 14 squawk [15] | 15 spi [16] (bool)
 --   16 position_source [17] (0 ADS-B,1 ASTERIX,2 MLAT,3 FLARM) | 17 category [18] (0-20)
+--
+-- NOTE: Some upstream snapshots dropped the sensors field (pos 13), producing 17-field vectors.
+-- When len(state)=17, fields from geo_altitude onward shift left by one position:
+--   geo_altitude=[13], squawk=[14], spi=[15], position_source=[16], category=[17].
+-- When len(state)=18, the original layout is preserved with sensors=[13].
 -- Rebuilt in full on every run (CREATE OR REPLACE) so a fix here backfills history automatically.
 CREATE OR REPLACE TABLE silver_flights AS
@@ -25,9 +30,24 @@
     TRY_CAST(state[11] AS DOUBLE)           AS true_track,
     TRY_CAST(state[12] AS DOUBLE)           AS vertical_rate,
-    TRY_CAST(state[14] AS DOUBLE)           AS geo_altitude,
-    state[15]                               AS squawk,
-    TRY_CAST(state[16] AS BOOLEAN)          AS spi,
-    TRY_CAST(state[17] AS INTEGER)          AS position_source,
-    TRY_CAST(state[18] AS INTEGER)          AS category,
+    CASE
+        WHEN len(state) = 17 THEN TRY_CAST(state[13] AS DOUBLE)
+        ELSE TRY_CAST(state[14] AS DOUBLE)
+    END                                     AS geo_altitude,
+    CASE
+        WHEN len(state) = 17 THEN state[14]
+        ELSE state[15]
+    END                                     AS squawk,
+    CASE
+        WHEN len(state) = 17 THEN TRY_CAST(state[15] AS BOOLEAN)
+        ELSE TRY_CAST(state[16] AS BOOLEAN)
+    END                                     AS spi,
+    CASE
+        WHEN len(state) = 17 THEN TRY_CAST(state[16] AS INTEGER)
+        ELSE TRY_CAST(state[17] AS INTEGER)
+    END                                     AS position_source,
+    CASE
+        WHEN len(state) = 17 THEN TRY_CAST(state[17] AS INTEGER)
+        ELSE TRY_CAST(state[18] AS INTEGER)
+    END                                     AS category,
     len(state)                              AS field_count
 FROM bronze_states;
```

Sandbox: failing run 15/15 checks passed; history 13/13 passed.  
Metrics before: `{'rows': 2612, 'airborne': 2359, 'mean_velocity_ms': 209.1, 'pct_with_position': 100.0}`  
Metrics after: `{'rows': 2612, 'airborne': 2359, 'mean_velocity_ms': 209.1, 'pct_with_position': 100.0}`

## Checks on prod after the fix

| check | status | failed/total | detail |
|---|---|---:|---|
| airborne_have_velocity | passed | 0/2612 |  |
| baro_altitude_in_range | passed | 0/2612 | min=-38.1 median=10294.619999999999 max=13716.0 |
| category_present | passed | 0/2612 |  |
| freshness | passed | 0/2612 | snapshot_time=1792962290 now=1792962486 age_s=196 |
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
| diagnosis | qwen3.7-plus | 8 | 28247/7179 | terminal_tool |

Total tokens: 28247 in / 7179 out.

---

**Disclosure:** this incident was produced by Autopilot's chaos module (`drop_field_sensors`), which injects a realistic upstream fault into a live OpenSky snapshot. The agents had no access to this label; they diagnosed and fixed it from the data alone.
