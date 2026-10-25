-- silver_flights: typed unpack of OpenSky state vectors from bronze_states.
--
-- IMPORTANT: DuckDB list indexing is 1-BASED. state[1] = icao24 ... state[18] = category.
-- OpenSky /states/all field order (0-based API index -> 1-based list index shown in brackets):
--   0 icao24 [1] | 1 callsign [2] | 2 origin_country [3] | 3 time_position [4] (unix s) | 4 last_contact [5] (unix s)
--   5 longitude [6] (deg) | 6 latitude [7] (deg) | 7 baro_altitude [8] (m) | 8 on_ground [9] (bool)
--   9 velocity [10] (m/s ground speed) | 10 true_track [11] (deg) | 11 vertical_rate [12] (m/s)
--   12 sensors [13] (int[] or null) | 13 geo_altitude [14] (m) | 14 squawk [15] | 15 spi [16] (bool)
--   16 position_source [17] (0 ADS-B,1 ASTERIX,2 MLAT,3 FLARM) | 17 category [18] (0-20)
-- Rebuilt in full on every run (CREATE OR REPLACE) so a fix here backfills history automatically.
CREATE OR REPLACE TABLE silver_flights AS
SELECT
    run_id,
    snapshot_time,
    state[1]                                AS icao24,
    nullif(trim(state[2]), '')              AS callsign,
    state[3]                                AS origin_country,
    TRY_CAST(state[4]  AS BIGINT)           AS time_position,
    TRY_CAST(state[5]  AS BIGINT)           AS last_contact,
    TRY_CAST(state[6]  AS DOUBLE)           AS longitude,
    TRY_CAST(state[7]  AS DOUBLE)           AS latitude,
    TRY_CAST(state[8]  AS DOUBLE)           AS baro_altitude,
    TRY_CAST(state[9]  AS BOOLEAN)          AS on_ground,
    TRY_CAST(state[10] AS DOUBLE)           AS velocity,
    TRY_CAST(state[11] AS DOUBLE)           AS true_track,
    TRY_CAST(state[12] AS DOUBLE)           AS vertical_rate,
    TRY_CAST(state[14] AS DOUBLE)           AS geo_altitude,
    state[15]                               AS squawk,
    TRY_CAST(state[16] AS BOOLEAN)          AS spi,
    TRY_CAST(state[17] AS INTEGER)          AS position_source,
    TRY_CAST(state[18] AS INTEGER)          AS category,
    len(state)                              AS field_count
FROM bronze_states;
