-- gold_snapshot_metrics: one row per snapshot; the "dashboard numbers".
CREATE OR REPLACE TABLE gold_snapshot_metrics AS
SELECT
    snapshot_time,
    to_timestamp(snapshot_time)::TIMESTAMP                        AS snapshot_ts,
    count(*)                                                      AS aircraft_total,
    count(*) FILTER (WHERE on_ground = false)                     AS airborne,
    round(avg(velocity)      FILTER (WHERE on_ground = false), 1) AS mean_velocity_ms,
    round(avg(baro_altitude) FILTER (WHERE on_ground = false), 0) AS mean_baro_altitude_m,
    round(100.0 * count(latitude) / count(*), 1)                  AS pct_with_position,
    count(DISTINCT origin_country)                                AS countries
FROM silver_flights
GROUP BY snapshot_time
ORDER BY snapshot_time;
