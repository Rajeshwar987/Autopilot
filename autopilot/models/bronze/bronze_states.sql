-- bronze_states: lossless landing table. One row per aircraft per snapshot.
-- `state` keeps the raw OpenSky state vector as a list of strings (NULL preserved), untyped on purpose:
-- a positional shift or a unit change upstream still loads here and is caught downstream in silver,
-- where it can be fixed with SQL.
CREATE TABLE IF NOT EXISTS bronze_states (
    run_id        VARCHAR NOT NULL,
    snapshot_time BIGINT  NOT NULL,
    ingested_at   TIMESTAMP NOT NULL,
    state         VARCHAR[] NOT NULL
);
