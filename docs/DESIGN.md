# Design

## The question

Given a year of taxi trips (38.3M rows, 4.5 GB) and twelve questions an analyst would ask,
how fast can PostgreSQL answer them, what does each technique buy, and what does it cost? And
how does MySQL 8 handle the same design?

## Method

- **Same data, same server settings, same SQL semantics.** The baseline and tuned PostgreSQL
  runs share `docker-compose.yml` settings. The tuned SQL differs from the baseline only where a
  materialized view replaces a scan of the trips table.
- **Same answers first.** Every query's result is normalized and fingerprinted. The tuned
  design must match the baseline exactly; MySQL must match within one cent (its DECIMAL division
  keeps 4 extra digits). A speedup that changes the answer isn't a speedup.
- **Warm cache, medians.** One warm-up run, then 5 timed runs; the median is reported. The
  question is what the *design* costs, so disk speed is kept out of it. Page accesses from
  `EXPLAIN (ANALYZE, BUFFERS)` show the same thing independently of caching.
- **Every number has a plan.** `docs/plans/<design>/<query>.txt` holds the SQL and the
  `EXPLAIN ANALYZE` output each timing came from.
- **Exact types.** Money and distance are `numeric(10,2)`/`numeric(9,2)`. Sums are then
  order-independent, so parallel plans, MVs and MySQL all produce the same cents. Floats would
  differ in the last digit depending on summation order.

## Loading

The monthly Parquet files disagree: January uses int64/double where later months use
int32/int64, and the airport fee is `airport_fee` in January and `Airport_fee` afterwards.
`source.py` normalizes every month to one Arrow schema (rounding the source doubles to cents
before the decimal cast). Arrow writes CSV in C and streams it into `COPY ... FROM STDIN`, so
Python never handles individual rows: 677k rows/s.

## The tuned design, technique by technique

### 1. Monthly range partitioning

`trips` is `PARTITION BY RANGE (pickup_at)` with 12 monthly partitions and a `DEFAULT` partition
(104 rows with impossible dates: 2002, 2008, 2024). With constant date predicates, the planner
removes every irrelevant partition at plan time (a test asserts exactly which partitions each
query touches). Partitions also make retention and maintenance cheap: dropping a month is a
metadata operation, and VACUUM works one month at a time.

### 2. BRIN on pickup_at

A BRIN index stores the min/max `pickup_at` per range of 32 pages. That's useful only when the
column follows physical order; here it does (correlation 0.9996 even unsorted, 1.0 after loading
each month sorted). The whole index is 832 kB, against 1.1 GB for a B-tree on a similar key. It
narrows a one-week scan to 2% of the table's pages. The trade-off is lossy bitmaps (rechecking
every row in each matching block), and it stops working if updates scatter old dates across
the table.

### 3. B-tree (pu_location_id, pickup_at)

Serves "one zone, a time range" (q10). Equality column first, range column second. Also built
on every partition, so pruning and the index combine.

### 4. Covering index (pu_location_id, do_location_id) INCLUDE (...)

The route query needs only six columns. With them in the index and the visibility map set by
VACUUM, it's an index-only scan with zero heap fetches. `INCLUDE` columns are stored in leaf
pages only, so they don't widen the search keys. At 2.1 GB it's the most expensive structure
here; it's justified only by frequent route queries. The planner also used it for q08, as the
narrowest way to scan a month for two zone columns.

### 5. Materialized views

- `mv_daily`: day × vendor × pickup zone × payment type, with counts and sums. Full-year
  aggregates (q01, q07, q09) and a month's top zones (q03) read 3–30k pages instead of 580k.
  Sums of sums are exact because of the numeric types.
- `mv_hourly_zone`: day × hour × zone counts (q04).
- `mv_duration_hist`, `mv_tip_hist`: **exact percentiles without sorting.** One row per distinct
  value with its count. `percentile_disc(p)` is the first value whose cumulative share reaches
  p, which is `min(value) FILTER (WHERE running_count >= p * total)` over the histogram. It's the
  same value, from 28k or 218 rows instead of a 3–8M-row sort.

Each MV has a unique index, which `REFRESH MATERIALIZED VIEW CONCURRENTLY` requires: readers keep
the old contents during the refresh (44 s for all four). The price is staleness between
refreshes, plus the refresh cost.

## What wasn't done, and why

- **Server tuning between runs**: deliberately excluded, to isolate schema design.
- **A primary key on trips**: the data has no natural key, and on a partitioned table a primary
  key must include the partition key. A surrogate key would add 0.8 GB and helps no query here.
- **Cold-cache timings**: on a laptop they measure the SSD. Page accesses show the I/O
  difference instead.
- **Columnar storage** (Citus columnar, DuckDB): the obvious next step for this workload, but
  outside "tune PostgreSQL".

## MySQL equivalent

| PostgreSQL | MySQL 8.4 |
|---|---|
| RANGE partitions + DEFAULT | RANGE COLUMNS partitions + `p_old` / `p_future` |
| BRIN | none → B-tree on `pickup_at` |
| `INCLUDE` covering index | none → all six columns as key columns |
| materialized views | none → summary tables from the same SELECT, clustered on their primary key |
| `percentile_disc` | none → `ROW_NUMBER()` over sorted values, `MIN(CASE WHEN rn >= CEIL(p*n))` |
| parallel query | none for these queries |
| hash joins chosen | nested loops chosen when an index exists |

The plan-by-plan differences are in [RESULTS.md](RESULTS.md#mysql-84-where-the-planners-diverge).

## Running it on a laptop

Docker Desktop's VM had about 20 GB free: not enough for the baseline heap, the partitioned
table, its indexes, PostgreSQL's WAL (up to `max_wal_size`) and a MySQL copy at the same time. So
the benchmark runs in phases: load and benchmark both PostgreSQL designs → save results and plans
→ drop them → load and benchmark MySQL → compare. `max_wal_size` is 1 GB for the same reason.
