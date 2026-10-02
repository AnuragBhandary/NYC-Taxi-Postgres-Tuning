# Results

38,310,226 NYC TLC yellow-taxi trips (all of 2023), PostgreSQL 16 and MySQL 8.4 in Docker on an
Apple M5 Pro laptop (Docker VM: 12 CPUs, 11.7 GB RAM). Server settings are identical for both
PostgreSQL designs (see `docker-compose.yml`). Raw data: [results.json](results.json),
[bench-pg-baseline.json](bench-pg-baseline.json), [bench-pg-tuned.json](bench-pg-tuned.json),
[bench-mysql.json](bench-mysql.json). Every plan: [plans/](plans/).

Each figure is the **median of 5 runs after a warm-up run** (warm cache). **Every query returned
the same rows on all three designs** (exact for the two PostgreSQL designs, within one cent for
MySQL), checked before any timing was reported.

## Suite

| | PostgreSQL baseline | PostgreSQL tuned | MySQL 8.4 (equivalent design) |
|---|---|---|---|
| **Median query** | **488 ms** | **19.4 ms** (25× faster) | 47.2 ms |
| Whole suite (12 queries) | 19.0 s | 0.45 s (42× faster) | 2.16 s |

## Per query

Page accesses come from `EXPLAIN (ANALYZE, BUFFERS)`: 8 KB buffer accesses, counting repeat
visits.

| # | Question | Mechanism | Baseline | Tuned | Speedup | MySQL | Pages: baseline → tuned |
|---|---|---|---|---|---|---|---|
| q01 | Revenue per month, full year | daily MV | 4,959 ms | 25.1 ms | 198× | 110 ms | 579,504 → 3,600 |
| q02 | Daily stats, one March week | pruning + BRIN | 378 ms | 122.9 ms | 3.1× | 560 ms | 579,732 → 11,818 |
| q03 | Top 10 zones by revenue, March | daily MV | 481 ms | 8.4 ms | 57× | 16 ms | 579,547 → 30,814 |
| q04 | JFK by weekday × hour, full year | hourly rollup | 496 ms | 3.3 ms | 150× | 2.2 ms | 579,728 → 4,492 |
| q05 | p50/p90 duration by borough, June | duration histogram | 934 ms | 16.2 ms | 58× | 38 ms | 579,863 → 27,961 |
| q06 | Tip % quartiles + p95, Q2 | tip histogram | 2,412 ms | 10.8 ms | 223× | 29 ms | 579,732 → 218 |
| q07 | 7-day moving average, full year | daily MV | 2,887 ms | 18.5 ms | 156× | 41 ms | 579,504 → 3,600 |
| q08 | Top 3 drop-offs per borough, June | pruning + covering index | 443 ms | 162.8 ms | 2.7× | 1,063 ms | 579,610 → 63,953 |
| q09 | Vendor revenue, MoM growth | daily MV | 4,843 ms | 31.0 ms | 156× | 107 ms | 579,504 → 3,614 |
| q10 | Airports by hour, one July week | pruning + zone index | 354 ms | 20.3 ms | 17× | 45 ms | 579,756 → 3,028 |
| q11 | Midtown → JFK route by month | covering index, index-only | 356 ms | 7.7 ms | 46× | 50 ms | 579,732 → 248 |
| q12 | Fare outliers, Thanksgiving | pruning + BRIN | 428 ms | 27.1 ms | 16× | 97 ms | 579,732 → 1,038 |

The baseline reads the whole 4.5 GB table for every query (~580k pages) with up to 5 parallel
workers. That's why even its "small" queries take 350 ms and none is faster.

## Where the time went

**Pruning + BRIN (q02, q12).** For q02 the planner keeps only `trips_2023_03`. The BRIN index
(832 kB for the whole table) reads 10 index pages and turns the scan into a lossy bitmap over
11.8k heap pages instead of 580k. What's left is aggregating 775k rows, single-threaded. A week
is 23% of a month, which is why q02 "only" gets 3×. q12 (one day) gets 16×.

**Covering index (q11).** `(pu_location_id, do_location_id) INCLUDE (pickup_at, dropoff_at,
fare_amount, total_amount)` turns the route query into an index-only scan with 0 heap fetches
(the table was vacuumed, so the visibility map is set): 248 pages. The cost is 2.1 GB of index.

**The covering index helps a query it wasn't built for (q08).** The planner scans June through
the route index (parallel index-only scan) because it holds both zone columns and is narrower
than the table.

**Materialized views for full-year aggregates (q01, q03, q07, q09).** 38M rows become
`mv_daily` (one row per day × vendor × zone × payment type, 39 MB). A whole-year question reads
3,600 pages.

**Exact percentiles from histograms (q05, q06).** `percentile_disc` sorts every row in a single
process: 3.3M rows for q05 (spilling 133 MB to disk) and 7.9M for q06. Pre-aggregating to one
row per distinct value with its count, then taking `min(value) FILTER (WHERE running_count >=
p * total)`, returns the identical value from 28k and 218 pages.

## Costs of the tuned design

| | |
|---|---|
| Extra storage | BRIN 0.8 MB · zone/time B-tree 1.1 GB · covering route index 2.1 GB · 4 MVs 146 MB |
| Build time after load | BRIN 5 s · B-trees 12 s + 14 s · MVs 36 s |
| Freshness | `REFRESH MATERIALIZED VIEW CONCURRENTLY`: 44 s for all four (readers keep the old version meanwhile) |
| Writes | every insert maintains 3 indexes; MVs are stale until refreshed |

BRIN is the bargain: 0.8 MB against 1.1 GB for the B-tree, because pickup time follows physical
order (correlation 0.9996 even in the raw file order). The 2.1 GB covering index is only worth
it if route-style questions are frequent.

## Load

| | Rows | Time | Rows/s |
|---|---|---|---|
| PostgreSQL `COPY` (Arrow → CSV stream, unpartitioned) | 38,310,226 | 57 s | 677k |
| MySQL `LOAD DATA LOCAL INFILE` (partitioned) | 38,310,226 | 114 s | 336k |

Into 12 monthly partitions plus a DEFAULT partition, which caught 104 trips with impossible
dates (2002, 2008, 2024...).

## MySQL 8.4: where the planners diverge

MySQL got the closest equivalent design: the same monthly partitions; a B-tree on `pickup_at` in
place of BRIN; the same composite indexes (the covering one as six key columns, since MySQL has
no `INCLUDE`); and the four MVs as summary tables built with the same SQL. All 12 answers match
PostgreSQL.

| Query | MySQL | What its plan does differently |
|---|---|---|
| q02 | 560 ms vs 123 ms | Prunes to March, then **scans all 3.4M rows in one thread**. It's right not to use its B-tree on `pickup_at`: with 23% of the month matching, each hit would cost a second lookup through the clustered key. PostgreSQL's BRIN has no per-row lookup cost. |
| q08 | 1,063 ms vs 163 ms | **Nested-loop join driven by the 265-row zones table**: per zone, a range scan of `(pu_location_id, pickup_at)`, then a clustered-key lookup and a primary-key probe of zones per trip, 3.3M times, single-threaded, aggregated in a temporary table. PostgreSQL: parallel index-only scan, hash joins, partial aggregation in 5 workers. |
| q11 | 50 ms vs 7.7 ms | Index range scan is fine, but the percentile emulation (ROW_NUMBER over sorted values, twice) **materializes each CTE into a temporary table**. |
| q04 | 2.2 ms vs 3.3 ms | **MySQL wins**: the summary table is clustered on its primary key `(pu_location_id, day, hour)`, so JFK's year is one contiguous range. The PostgreSQL MV is a heap with a separate index. |
| q01, q09 | 110 ms vs 25–31 ms | Both read the 39 MB summary table; PostgreSQL's hash aggregation over it is faster than MySQL's temporary-table GROUP BY. |

## Bugs the result-equality check caught

1. **MySQL lost half the zone lookup table, silently.** The TLC CSV has Windows line endings.
   With `LOAD DATA`'s default `\n`, every quoted last field ended in `"\r`, the quote never
   closed, and the next row was swallowed: 133 of 265 zones. Four queries returned wrong
   answers, with no error. Fix: `LINES TERMINATED BY '\r\n'`, plus a row-count check after the
   load. PostgreSQL's CSV parser accepts CRLF, so only MySQL was affected.
2. **The test sample didn't exercise q12** (no Thanksgiving zone had enough trips for a 3-sigma
   outlier), so a broken q12 would have passed. The test now requires every query to return
   rows, and the sample includes a full Thanksgiving day for two zones.
