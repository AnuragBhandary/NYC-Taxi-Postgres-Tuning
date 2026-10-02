"""The analytical suite: 12 questions an analyst would ask of a year of taxi trips.

Each query has
  base    PostgreSQL SQL with {t} for the trips table (trips_heap for the baseline run; the
          partitioned `trips` for the tuned run)
  tuned   only where a materialized view replaces a full-year scan; otherwise the tuned run
          executes `base` against the partitioned table, so the speedup comes purely from
          partition pruning and indexes
  mysql   the same question in MySQL 8 dialect, on the equivalent MySQL design

Percentiles use percentile_disc (an actual value from the data, no interpolation), so MySQL can
reproduce them exactly with ROW_NUMBER() over the sorted values. Money is exact numeric, so every
variant must return the *same rows*: the benchmark checks that before it reports a speedup.
"""

from __future__ import annotations

from dataclasses import dataclass

YEAR = "pickup_at >= '2023-01-01' AND pickup_at < '2024-01-01'"
AIRPORTS = "(1, 132, 138)"  # EWR, JFK, LaGuardia


@dataclass(frozen=True)
class Query:
    id: str
    title: str
    exercises: str
    base: str
    mysql: str
    tuned: str | None = None

    def pg(self, design: str) -> str:
        if design == "tuned":
            return self.tuned or self.base.format(t="trips")
        return self.base.format(t="trips_heap")


def _mysql_pct(p: float, col: str = "v") -> str:
    """percentile_disc(p) = the value at position ceil(p * n) of the sorted group."""
    return f"MIN(CASE WHEN rn >= CEIL({p} * n) THEN {col} END)"


SUITE: list[Query] = [
    Query(
        "q01_monthly_revenue",
        "Trips and revenue per month, whole year",
        "full-year aggregate -> materialized view",
        base=f"""SELECT date_trunc('month', pickup_at)::date AS month, count(*) AS trips,
                        sum(total_amount) AS revenue
                 FROM {{t}} WHERE {YEAR} GROUP BY 1 ORDER BY 1""",
        tuned="""SELECT date_trunc('month', day)::date AS month, sum(trips)::bigint AS trips,
                        sum(revenue) AS revenue
                 FROM mv_daily WHERE day >= '2023-01-01' AND day < '2024-01-01'
                 GROUP BY 1 ORDER BY 1""",
        mysql="""SELECT CAST(DATE_FORMAT(day, '%Y-%m-01') AS DATE) AS month, SUM(trips) AS trips,
                         SUM(revenue) AS revenue
                  FROM mv_daily WHERE day >= '2023-01-01' AND day < '2024-01-01'
                  GROUP BY 1 ORDER BY 1""",
    ),
    Query(
        "q02_week_daily",
        "Daily trips, average distance and fare for one week of March",
        "7-day range -> partition pruning + BRIN",
        base="""SELECT pickup_at::date AS day, count(*) AS trips,
                       round(sum(trip_distance) / count(*), 2) AS avg_miles,
                       round(sum(fare_amount) / count(*), 2) AS avg_fare
                FROM {t} WHERE pickup_at >= '2023-03-06' AND pickup_at < '2023-03-13'
                GROUP BY 1 ORDER BY 1""",
        mysql="""SELECT DATE(pickup_at) AS day, COUNT(*) AS trips,
                        ROUND(SUM(trip_distance) / COUNT(*), 2) AS avg_miles,
                        ROUND(SUM(fare_amount) / COUNT(*), 2) AS avg_fare
                 FROM trips WHERE pickup_at >= '2023-03-06' AND pickup_at < '2023-03-13'
                 GROUP BY 1 ORDER BY 1""",
    ),
    Query(
        "q03_top_zones_march",
        "Top 10 pickup zones by revenue in March",
        "one month + join + top-N -> materialized view",
        base="""SELECT z.location_id, z.zone, z.borough, count(*) AS trips,
                       sum(t.total_amount) AS revenue
                FROM {t} t JOIN zones z ON z.location_id = t.pu_location_id
                WHERE t.pickup_at >= '2023-03-01' AND t.pickup_at < '2023-04-01'
                GROUP BY 1, 2, 3 ORDER BY revenue DESC, z.location_id LIMIT 10""",
        tuned="""SELECT z.location_id, z.zone, z.borough, sum(m.trips)::bigint AS trips,
                        sum(m.revenue) AS revenue
                 FROM mv_daily m JOIN zones z ON z.location_id = m.pu_location_id
                 WHERE m.day >= '2023-03-01' AND m.day < '2023-04-01'
                 GROUP BY 1, 2, 3 ORDER BY revenue DESC, z.location_id LIMIT 10""",
        mysql="""SELECT z.location_id, z.zone, z.borough, SUM(m.trips) AS trips,
                        SUM(m.revenue) AS revenue
                 FROM mv_daily m JOIN zones z ON z.location_id = m.pu_location_id
                 WHERE m.day >= '2023-03-01' AND m.day < '2023-04-01'
                 GROUP BY 1, 2, 3 ORDER BY revenue DESC, z.location_id LIMIT 10""",
    ),
    Query(
        "q04_jfk_hourly_profile",
        "JFK pickups by weekday and hour, whole year",
        "one zone, whole year -> hourly rollup (was: index-only scan)",
        base=f"""SELECT extract(isodow FROM pickup_at)::int AS dow,
                        extract(hour FROM pickup_at)::int AS hour, count(*) AS trips
                 FROM {{t}} WHERE pu_location_id = 132 AND {YEAR}
                 GROUP BY 1, 2 ORDER BY 1, 2""",
        tuned="""SELECT extract(isodow FROM day)::int AS dow, hour, sum(trips)::bigint AS trips
                 FROM mv_hourly_zone WHERE pu_location_id = 132
                   AND day >= '2023-01-01' AND day < '2024-01-01'
                 GROUP BY 1, 2 ORDER BY 1, 2""",
        mysql="""SELECT WEEKDAY(day) + 1 AS dow, hour, SUM(trips) AS trips
                 FROM mv_hourly_zone WHERE pu_location_id = 132
                   AND day >= '2023-01-01' AND day < '2024-01-01'
                 GROUP BY 1, 2 ORDER BY 1, 2""",
    ),
    Query(
        "q05_duration_pct_by_borough",
        "Median and p90 trip duration by pickup borough, June",
        "one month, exact percentiles -> duration histogram (was: 3.3M-row sort)",
        base="""SELECT z.borough, count(*) AS trips,
                       percentile_disc(0.5) WITHIN GROUP (ORDER BY
                           extract(epoch FROM t.dropoff_at - t.pickup_at)::int) AS p50_s,
                       percentile_disc(0.9) WITHIN GROUP (ORDER BY
                           extract(epoch FROM t.dropoff_at - t.pickup_at)::int) AS p90_s
                FROM {t} t JOIN zones z ON z.location_id = t.pu_location_id
                WHERE t.pickup_at >= '2023-06-01' AND t.pickup_at < '2023-07-01'
                GROUP BY 1 ORDER BY 1""",
        tuned="""WITH h AS (
                    SELECT borough, duration_s, n,
                           sum(n) OVER (PARTITION BY borough ORDER BY duration_s) AS cum,
                           sum(n) OVER (PARTITION BY borough) AS total
                    FROM mv_duration_hist WHERE month = '2023-06-01')
                 SELECT borough, max(total)::bigint AS trips,
                        min(duration_s) FILTER (WHERE cum >= 0.5 * total) AS p50_s,
                        min(duration_s) FILTER (WHERE cum >= 0.9 * total) AS p90_s
                 FROM h GROUP BY 1 ORDER BY 1""",
        mysql="""WITH h AS (
                    SELECT borough, duration_s, n,
                           SUM(n) OVER (PARTITION BY borough ORDER BY duration_s) AS cum,
                           SUM(n) OVER (PARTITION BY borough) AS total
                    FROM mv_duration_hist WHERE month = '2023-06-01')
                 SELECT borough, MAX(total) AS trips,
                        MIN(CASE WHEN cum >= 0.5 * total THEN duration_s END) AS p50_s,
                        MIN(CASE WHEN cum >= 0.9 * total THEN duration_s END) AS p90_s
                 FROM h GROUP BY borough ORDER BY borough""",
    ),
    Query(
        "q06_tip_pct_quartiles",
        "Credit-card tip % quartiles and p95 by month, Q2",
        "three months, four exact percentiles -> tip histogram (was: 7.9M-row sort)",
        base="""SELECT date_trunc('month', pickup_at)::date AS month, count(*) AS trips,
                       percentile_disc(0.25) WITHIN GROUP (ORDER BY tip_pct) AS p25,
                       percentile_disc(0.5) WITHIN GROUP (ORDER BY tip_pct) AS p50,
                       percentile_disc(0.75) WITHIN GROUP (ORDER BY tip_pct) AS p75,
                       percentile_disc(0.95) WITHIN GROUP (ORDER BY tip_pct) AS p95
                FROM (SELECT pickup_at, round(tip_amount * 100 / fare_amount, 2) AS tip_pct
                      FROM {t} WHERE payment_type = 1 AND fare_amount > 0
                        AND pickup_at >= '2023-04-01' AND pickup_at < '2023-07-01') x
                GROUP BY 1 ORDER BY 1""",
        tuned="""WITH h AS (
                    SELECT month, tip_pct, n,
                           sum(n) OVER (PARTITION BY month ORDER BY tip_pct) AS cum,
                           sum(n) OVER (PARTITION BY month) AS total
                    FROM mv_tip_hist WHERE month >= '2023-04-01' AND month < '2023-07-01')
                 SELECT month, max(total)::bigint AS trips,
                        min(tip_pct) FILTER (WHERE cum >= 0.25 * total) AS p25,
                        min(tip_pct) FILTER (WHERE cum >= 0.5 * total) AS p50,
                        min(tip_pct) FILTER (WHERE cum >= 0.75 * total) AS p75,
                        min(tip_pct) FILTER (WHERE cum >= 0.95 * total) AS p95
                 FROM h GROUP BY 1 ORDER BY 1""",
        mysql="""WITH h AS (
                    SELECT month, tip_pct, n,
                           SUM(n) OVER (PARTITION BY month ORDER BY tip_pct) AS cum,
                           SUM(n) OVER (PARTITION BY month) AS total
                    FROM mv_tip_hist WHERE month >= '2023-04-01' AND month < '2023-07-01')
                 SELECT month, MAX(total) AS trips,
                        MIN(CASE WHEN cum >= 0.25 * total THEN tip_pct END) AS p25,
                        MIN(CASE WHEN cum >= 0.5 * total THEN tip_pct END) AS p50,
                        MIN(CASE WHEN cum >= 0.75 * total THEN tip_pct END) AS p75,
                        MIN(CASE WHEN cum >= 0.95 * total THEN tip_pct END) AS p95
                 FROM h GROUP BY month ORDER BY month""",
    ),
    Query(
        "q07_rolling_7day",
        "Daily trips with 7-day moving average and week-over-week change",
        "full-year daily series + window functions -> materialized view",
        base=f"""WITH d AS (SELECT pickup_at::date AS day, count(*) AS trips
                            FROM {{t}} WHERE {YEAR} GROUP BY 1)
                 SELECT day, trips,
                        round(avg(trips) OVER (ORDER BY day ROWS BETWEEN 6 PRECEDING
                                               AND CURRENT ROW), 2) AS ma7,
                        trips - lag(trips, 7) OVER (ORDER BY day) AS wow_change
                 FROM d ORDER BY day""",
        tuned="""WITH d AS (SELECT day, sum(trips)::bigint AS trips FROM mv_daily
                           WHERE day >= '2023-01-01' AND day < '2024-01-01' GROUP BY 1)
                 SELECT day, trips,
                        round(avg(trips) OVER (ORDER BY day ROWS BETWEEN 6 PRECEDING
                                               AND CURRENT ROW), 2) AS ma7,
                        trips - lag(trips, 7) OVER (ORDER BY day) AS wow_change
                 FROM d ORDER BY day""",
        mysql="""WITH d AS (SELECT day, SUM(trips) AS trips FROM mv_daily
                           WHERE day >= '2023-01-01' AND day < '2024-01-01' GROUP BY 1)
                 SELECT day, trips,
                        ROUND(AVG(trips) OVER (ORDER BY day ROWS BETWEEN 6 PRECEDING
                                               AND CURRENT ROW), 2) AS ma7,
                        trips - LAG(trips, 7) OVER (ORDER BY day) AS wow_change
                 FROM d ORDER BY day""",
    ),
    Query(
        "q08_top3_dropoffs",
        "Top 3 drop-off zones for each pickup borough, June",
        "one month -> pruning; two joins, CTE, ROW_NUMBER",
        base="""WITH c AS (
                    SELECT pz.borough AS pu_borough, dz.zone AS do_zone, count(*) AS trips
                    FROM {t} t
                    JOIN zones pz ON pz.location_id = t.pu_location_id
                    JOIN zones dz ON dz.location_id = t.do_location_id
                    WHERE t.pickup_at >= '2023-06-01' AND t.pickup_at < '2023-07-01'
                    GROUP BY 1, 2),
                r AS (SELECT *, row_number() OVER (PARTITION BY pu_borough
                                                   ORDER BY trips DESC, do_zone) AS rn FROM c)
                SELECT pu_borough, rn, do_zone, trips FROM r WHERE rn <= 3
                ORDER BY pu_borough, rn""",
        mysql="""WITH c AS (
                    SELECT pz.borough AS pu_borough, dz.zone AS do_zone, COUNT(*) AS trips
                    FROM trips t
                    JOIN zones pz ON pz.location_id = t.pu_location_id
                    JOIN zones dz ON dz.location_id = t.do_location_id
                    WHERE t.pickup_at >= '2023-06-01' AND t.pickup_at < '2023-07-01'
                    GROUP BY 1, 2),
                r AS (SELECT c.*, ROW_NUMBER() OVER (PARTITION BY pu_borough
                                                     ORDER BY trips DESC, do_zone) AS rn FROM c)
                SELECT pu_borough, rn, do_zone, trips FROM r WHERE rn <= 3
                ORDER BY pu_borough, rn""",
    ),
    Query(
        "q09_vendor_mom",
        "Monthly revenue per vendor with month-over-month growth",
        "full-year aggregate + LAG -> materialized view",
        base=f"""WITH m AS (SELECT date_trunc('month', pickup_at)::date AS month,
                                   coalesce(vendor_id, 0) AS vendor_id,
                                   sum(total_amount) AS revenue
                            FROM {{t}} WHERE {YEAR} GROUP BY 1, 2)
                 SELECT month, vendor_id, revenue,
                        round(100 * (revenue - lag(revenue) OVER w) / lag(revenue) OVER w, 2)
                            AS mom_pct
                 FROM m WINDOW w AS (PARTITION BY vendor_id ORDER BY month)
                 ORDER BY vendor_id, month""",
        tuned="""WITH m AS (SELECT date_trunc('month', day)::date AS month, vendor_id,
                                  sum(revenue) AS revenue
                           FROM mv_daily WHERE day >= '2023-01-01' AND day < '2024-01-01'
                           GROUP BY 1, 2)
                 SELECT month, vendor_id, revenue,
                        round(100 * (revenue - lag(revenue) OVER w) / lag(revenue) OVER w, 2)
                            AS mom_pct
                 FROM m WINDOW w AS (PARTITION BY vendor_id ORDER BY month)
                 ORDER BY vendor_id, month""",
        mysql="""WITH m AS (SELECT CAST(DATE_FORMAT(day, '%Y-%m-01') AS DATE) AS month, vendor_id,
                                  SUM(revenue) AS revenue
                           FROM mv_daily WHERE day >= '2023-01-01' AND day < '2024-01-01'
                           GROUP BY 1, 2)
                 SELECT month, vendor_id, revenue,
                        ROUND(100 * (revenue - LAG(revenue) OVER w) / LAG(revenue) OVER w, 2)
                            AS mom_pct
                 FROM m WINDOW w AS (PARTITION BY vendor_id ORDER BY month)
                 ORDER BY vendor_id, month""",
    ),
    Query(
        "q10_airports_week_hourly",
        "Airport pickups by hour for one July week",
        "3 zones x 7 days -> pruning + (pu_location_id, pickup_at) index",
        base=f"""SELECT pu_location_id, extract(hour FROM pickup_at)::int AS hour,
                        count(*) AS trips, round(sum(fare_amount) / count(*), 2) AS avg_fare,
                        round(sum(extract(epoch FROM dropoff_at - pickup_at)::int) / 60.0
                              / count(*), 1) AS avg_minutes
                 FROM {{t}} WHERE pu_location_id IN {AIRPORTS}
                   AND pickup_at >= '2023-07-03' AND pickup_at < '2023-07-10'
                 GROUP BY 1, 2 ORDER BY 1, 2""",
        mysql=f"""SELECT pu_location_id, HOUR(pickup_at) AS hour, COUNT(*) AS trips,
                         ROUND(SUM(fare_amount) / COUNT(*), 2) AS avg_fare,
                         ROUND(SUM(TIMESTAMPDIFF(SECOND, pickup_at, dropoff_at)) / 60.0
                               / COUNT(*), 1) AS avg_minutes
                  FROM trips WHERE pu_location_id IN {AIRPORTS}
                    AND pickup_at >= '2023-07-03' AND pickup_at < '2023-07-10'
                  GROUP BY 1, 2 ORDER BY 1, 2""",
    ),
    Query(
        "q11_route_midtown_jfk",
        "Midtown Center -> JFK by month: trips, median fare, p95 minutes",
        "one route, whole year -> covering index, index-only scan",
        base=f"""SELECT date_trunc('month', pickup_at)::date AS month, count(*) AS trips,
                        percentile_disc(0.5) WITHIN GROUP (ORDER BY fare_amount) AS median_fare,
                        percentile_disc(0.95) WITHIN GROUP (ORDER BY
                            extract(epoch FROM dropoff_at - pickup_at)::int) AS p95_s
                 FROM {{t}} WHERE pu_location_id = 161 AND do_location_id = 132 AND {YEAR}
                 GROUP BY 1 ORDER BY 1""",
        mysql=f"""WITH d AS (
                    SELECT CAST(DATE_FORMAT(pickup_at, '%Y-%m-01') AS DATE) AS month, fare_amount,
                           TIMESTAMPDIFF(SECOND, pickup_at, dropoff_at) AS secs
                    FROM trips WHERE pu_location_id = 161 AND do_location_id = 132 AND {YEAR}),
                  f AS (SELECT month, fare_amount AS v,
                               ROW_NUMBER() OVER (PARTITION BY month ORDER BY fare_amount) AS rn,
                               COUNT(*) OVER (PARTITION BY month) AS n FROM d),
                  s AS (SELECT month, secs AS v,
                               ROW_NUMBER() OVER (PARTITION BY month ORDER BY secs) AS rn,
                               COUNT(*) OVER (PARTITION BY month) AS n FROM d),
                  fa AS (SELECT month, n, {_mysql_pct(0.5)} AS median_fare
                         FROM f GROUP BY month, n),
                  sa AS (SELECT month, {_mysql_pct(0.95)} AS p95_s FROM s GROUP BY month)
                  SELECT fa.month, fa.n AS trips, fa.median_fare, sa.p95_s
                  FROM fa JOIN sa ON sa.month = fa.month ORDER BY fa.month""",
    ),
    Query(
        "q12_fare_outliers_thanksgiving",
        "Zones with the most fare-per-mile outliers (> mean + 3 sd) on Thanksgiving",
        "one day -> pruning + BRIN; CTE with window avg/stddev",
        base="""WITH x AS (
                    SELECT pu_location_id, round(fare_amount / trip_distance, 4) AS fpm
                    FROM {t} WHERE pickup_at >= '2023-11-23' AND pickup_at < '2023-11-24'
                      AND trip_distance >= 1 AND fare_amount > 0),
                s AS (SELECT pu_location_id, fpm,
                             avg(fpm) OVER (PARTITION BY pu_location_id) AS mu,
                             stddev_samp(fpm) OVER (PARTITION BY pu_location_id) AS sd
                      FROM x)
                SELECT z.zone, count(*) AS outliers
                FROM s JOIN zones z ON z.location_id = s.pu_location_id
                WHERE s.fpm > s.mu + 3 * s.sd
                GROUP BY z.zone ORDER BY outliers DESC, z.zone LIMIT 10""",
        mysql="""WITH x AS (
                    SELECT pu_location_id, ROUND(fare_amount / trip_distance, 4) AS fpm
                    FROM trips WHERE pickup_at >= '2023-11-23' AND pickup_at < '2023-11-24'
                      AND trip_distance >= 1 AND fare_amount > 0),
                s AS (SELECT pu_location_id, fpm,
                             AVG(fpm) OVER (PARTITION BY pu_location_id) AS mu,
                             STDDEV_SAMP(fpm) OVER (PARTITION BY pu_location_id) AS sd
                      FROM x)
                SELECT z.zone, COUNT(*) AS outliers
                FROM s JOIN zones z ON z.location_id = s.pu_location_id
                WHERE s.fpm > s.mu + 3 * s.sd
                GROUP BY z.zone ORDER BY outliers DESC, z.zone LIMIT 10""",
    ),
]

BY_ID = {q.id: q for q in SUITE}
