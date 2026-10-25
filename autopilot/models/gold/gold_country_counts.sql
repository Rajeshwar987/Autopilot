-- gold_country_counts: aircraft by registration country for the latest snapshot.
CREATE OR REPLACE TABLE gold_country_counts AS
SELECT
    snapshot_time,
    origin_country,
    count(*)                                                 AS aircraft,
    count(*) FILTER (WHERE on_ground = false)                AS airborne,
    round(avg(velocity) FILTER (WHERE on_ground = false), 1) AS mean_velocity_ms
FROM silver_flights
WHERE snapshot_time = (SELECT max(snapshot_time) FROM silver_flights)
GROUP BY snapshot_time, origin_country
ORDER BY aircraft DESC, origin_country;
