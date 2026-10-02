# PostgreSQL Query Tuning on 38M NYC Taxi Trips

[![CI](https://github.com/AnuragBhandary/NYC-Taxi-Postgres-Tuning/actions/workflows/ci.yml/badge.svg)](https://github.com/AnuragBhandary/NYC-Taxi-Postgres-Tuning/actions/workflows/ci.yml)
![PostgreSQL 16](https://img.shields.io/badge/PostgreSQL-16-336791)
![MySQL 8.4](https://img.shields.io/badge/MySQL-8.4-4479A1)

All **38,310,226** NYC TLC yellow-taxi trips of 2023 (4.5 GB) in PostgreSQL. Twelve analytical
questions are answered first by a plain table, then by a tuned physical design: **monthly
range partitions, BRIN, a composite and a covering B-tree, materialized views, and histogram
rollups for exact percentiles.** Every step is measured with `EXPLAIN (ANALYZE, BUFFERS)`, every
answer is checked to be identical, and the same suite runs on **MySQL 8.4**.

### Results ([details](docs/RESULTS.md), [every plan](docs/plans/))

| | PostgreSQL baseline | PostgreSQL tuned | MySQL 8.4 |
|---|---|---|---|
| **Median query** | 488 ms | **19.4 ms** (25×) | 47.2 ms |
| Whole suite | 19.0 s | **0.45 s** (42×) | 2.16 s |
| Same answers as baseline | - | **12 / 12, exact** | 12 / 12 (±1 cent) |

| Technique | Example | Effect |
|---|---|---|
| Partition pruning + **BRIN** (832 kB!) | one week of March | 580k → 12k pages read |
| **Covering index**, index-only scan | Midtown → JFK, full year | 356 → 7.7 ms, 248 pages, 0 heap fetches |
| **Materialized view** | revenue per month, full year | 4,959 → 25 ms |
| **Histogram rollup** for exact `percentile_disc` | tip % quartiles, Q2 | 2,412 → 10.8 ms (no 7.9M-row sort) |

What the tuning costs (3.4 GB of indexes, a 44 s concurrent MV refresh, staleness) and where
MySQL's planner diverges (nested loops vs hash joins, no parallel scans, clustered primary keys)
are in [RESULTS.md](docs/RESULTS.md). The reasoning is in [DESIGN.md](docs/DESIGN.md).

## Run it

```bash
make install            # uv sync
make data               # 12 monthly Parquet files from the TLC CDN (~600 MB) + zone lookup
make up                 # PostgreSQL 16 (fixed settings for every run)
make load-pg            # baseline table + partitioned table + indexes + MVs (~4 min)
make bench-pg           # 12 queries x (warm-up + 5 runs) on both designs, plans to docs/plans/
make mysql              # drops the baseline to free disk, loads MySQL 8.4, benchmarks it
make compare            # checks every design returned the same rows; prints the table
```

## Tests

`make test`: 41 tests, 95% coverage, on a committed 41k-trip sample (3,000 random trips per
month in the original Parquet schemas, plus a full Thanksgiving day for two zones):

- the monthly files normalize to one schema; money is exact
- **all 12 tuned queries return exactly the baseline's rows**, and all 12 MySQL queries match
- partition pruning: each date-bounded query touches exactly the expected partition
- rollup queries never touch the trips table
- the covering index gives an index-only scan; BRIN is chosen for the date range
- concurrent MV refresh; every CLI command end to end

## Layout

```
src/taxibench/  source (Parquet normalization), pg (designs + loader), mysql (equivalent design),
                queries (the 12 questions x 3 dialects), bench (timing, plans, fingerprints), cli
docs/           RESULTS, DESIGN, INTERVIEW, plans/<design>/<query>.txt, results.json
tests/          test_suite.py, fixtures/sample (Parquet)
```

Data: NYC Taxi & Limousine Commission trip records (public). Code: MIT.
