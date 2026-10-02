# Interview guide

How to learn this project, pitch it, and defend it. Numbers are in [RESULTS.md](RESULTS.md);
reasoning is in [DESIGN.md](DESIGN.md).

## 1. Learning path (read in this order)

| # | File | What to be able to explain afterwards |
|---|---|---|
| 1 | `src/taxibench/source.py` | The schema drift between months; why exact decimals; Arrow → CSV → COPY |
| 2 | `src/taxibench/pg.py` | Partition DDL, DEFAULT partition, each index and MV, why the unique indexes, VACUUM before benchmarking |
| 3 | `src/taxibench/queries.py` | Every query, which mechanism serves it, and the histogram-percentile trick |
| 4 | `docs/plans/pg-baseline/q02_week_daily.txt` vs `docs/plans/pg-tuned/q02_week_daily.txt` | Read both plans out loud: Gather, Parallel Seq Scan, Bitmap Index Scan, lossy heap blocks, Rows Removed by Recheck |
| 5 | `docs/plans/pg-tuned/q11_route_midtown_jfk.txt` | Index Only Scan, Heap Fetches: 0 and why it needs VACUUM |
| 6 | `src/taxibench/bench.py` | Warm-up, median, fingerprinting, why MySQL gets a 1-cent tolerance |
| 7 | `src/taxibench/mysql.py` + `docs/plans/mysql/q08_top3_dropoffs.txt` | Nested loop vs hash join; clustered primary key; no parallelism |
| 8 | `tests/test_suite.py` | Same-answer tests, the pruning assertions, forcing the access path on tiny data |

Exercise: open any baseline plan, cover the tuned one, and predict what the tuned plan does.

## 2. The 60-second pitch

> "I loaded all 38 million NYC yellow-taxi trips from 2023 into PostgreSQL and wrote twelve
> analytical queries: monthly revenue, percentiles of trip duration, top routes, a moving
> average, outlier detection. Then I tuned the physical design and measured every step with
> EXPLAIN ANALYZE. Monthly range partitions plus a BRIN index cut a one-week query from reading
> 580,000 pages to 12,000. A covering index made a route query index-only. Materialized views
> answered full-year aggregates, and pre-aggregated histograms gave exact percentiles without
> sorting millions of rows. The suite's median query went from 488 to 19 milliseconds, and every
> query was checked to return exactly the same rows as before. I ran the same design on MySQL 8:
> it got the same answers but was slower on the join-heavy queries, because it picked nested
> loops and has no parallel scans, and faster on one, because InnoDB clusters the summary table
> by its primary key."

## 3. Numbers to know cold

38.3M rows, 4.5 GB · load 57 s (677k rows/s) · median 488 → 19.4 ms (25×), suite 19.0 → 0.45 s ·
q02: 580k → 12k pages with an 832 kB BRIN · covering index 2.1 GB · MV refresh 44 s ·
MySQL median 47 ms, q08 1,063 vs 163 ms.

## 4. Likely questions

**"The baseline is already under half a second for most queries. Why tune?"** Because it reads
the full 4.5 GB every time and only stays fast while the table fits in memory and has the
machine to itself. Ten concurrent analysts, or a table twice the RAM, and every query becomes a
full disk scan. The tuned design reads 0.04–11% of the pages.

**"BRIN vs B-tree?"** BRIN stores min/max per block range, so it's tiny (832 kB vs 1.1 GB) and
cheap to maintain. It only works when the column follows physical order, which time-ordered
inserts give you. It returns *blocks*, so every row in them is rechecked. A B-tree gives exact
rows but is big, and with a non-covering index each hit costs a heap lookup.

**"Why did q02 only get 3× faster?"** A week is 23% of March. The BRIN bitmap reads exactly
those blocks, but then 775k rows still have to be aggregated, and the bitmap heap scan here ran
without parallel workers. The remaining cost is CPU, not I/O. A daily MV would make it ~10 ms;
I left it as the honest example of what pruning alone does.

**"What's an index-only scan and when does it fail?"** All needed columns are in the index, and
the visibility map says the page is all-visible, so the heap is never touched. Recently updated
pages aren't all-visible, so it falls back to heap fetches until VACUUM runs. That's why the
loader runs VACUUM (ANALYZE).

**"Materialized views go stale."** Yes: they're as fresh as the last refresh. `REFRESH ...
CONCURRENTLY` (needs a unique index) lets readers keep the old version during the 44 s refresh.
For a dashboard refreshed hourly that's fine. For real-time numbers you'd maintain summary tables
incrementally, or query the partitions directly.

**"How do you compute an exact percentile from a histogram?"** `percentile_disc(p)` returns the
first value whose cumulative share reaches p. With one row per distinct value and its count, a
running sum over the counts finds it: `min(value) FILTER (WHERE running >= p * total)`. It's
exact because the values are discrete (whole seconds, tip % to 2 decimals). With continuous
values you'd bucket them and accept the bucket width as the error.

**"How did you make sure you weren't measuring the cache?"** Warm-up then median of 5, so
every design is measured warm, and I also report page accesses from `EXPLAIN (BUFFERS)`,
which don't depend on caching.

**"Why is MySQL slower on q08 but faster on q04?"** q08: MySQL drove a nested loop from the
zones table into the trips index and did 3.3M secondary-index probes, each followed by a
clustered-key lookup, in one thread. PostgreSQL scanned a covering index in parallel and
hash-joined. q04: InnoDB tables are clustered on the primary key, so the summary table's rows for
JFK are physically contiguous; PostgreSQL's MV is a heap plus a separate index.

**"What went wrong?"** The MySQL zone table silently lost half its rows: Windows line endings
in the CSV made `LOAD DATA` swallow every other line. No error at all. The result-equality check
caught it, because four queries disagreed with PostgreSQL. Now the loader also checks the row
count.

**"What would you do next?"** Columnar storage for the scan-heavy queries; incremental summary
maintenance instead of full refreshes; `pg_stat_statements` on real traffic to choose which
indexes are worth their space; and testing at 10× the data on a cold cache.
