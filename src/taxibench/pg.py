"""PostgreSQL: the two physical designs being compared, and the loader.

baseline   trips_heap: one unpartitioned table, rows in file order, no indexes. What you get
           from a straight COPY of the files.
tuned      trips: RANGE-partitioned by month on pickup_at (12 partitions + a DEFAULT partition
           for the ~1k rows with impossible dates), each month loaded in pickup order, then
             - BRIN on pickup_at          (tiny; prunes blocks inside a partition)
             - B-tree (pu_location_id, pickup_at)               zone + time filters
             - B-tree (pu_location_id, do_location_id) INCLUDE (...)  covering: route queries
               become index-only scans
             - materialized view mv_daily: one row per day x vendor x pickup zone x payment
               type, which answers the whole-year aggregates from ~1M rows instead of 38M
           all built after the load (bulk load then index is much faster than the reverse).

Both designs use the same server settings (docker-compose.yml) and the same column types.
"""

from __future__ import annotations

import io
import itertools
import logging
import time
from pathlib import Path
from typing import Any

import psycopg
import pyarrow.csv as pacsv

from . import source

log = logging.getLogger(__name__)

COLUMNS_SQL = """
    vendor_id smallint,
    pickup_at timestamp NOT NULL,
    dropoff_at timestamp NOT NULL,
    passenger_count smallint,
    trip_distance numeric(9,2),
    ratecode_id smallint,
    store_and_fwd_flag char(1),
    pu_location_id smallint NOT NULL,
    do_location_id smallint NOT NULL,
    payment_type smallint,
    fare_amount numeric(10,2),
    extra numeric(10,2),
    mta_tax numeric(10,2),
    tip_amount numeric(10,2),
    tolls_amount numeric(10,2),
    improvement_surcharge numeric(10,2),
    total_amount numeric(10,2),
    congestion_surcharge numeric(10,2),
    airport_fee numeric(10,2)
"""

MONTHS = [f"2023-{m:02d}-01" for m in range(1, 13)] + ["2024-01-01"]

TUNING = [
    (
        "brin_pickup",
        "CREATE INDEX trips_pickup_brin ON trips USING brin (pickup_at)"
        " WITH (pages_per_range = 32)",
    ),
    ("btree_zone_time", "CREATE INDEX trips_pu_pickup ON trips (pu_location_id, pickup_at)"),
    (
        "btree_route_covering",
        "CREATE INDEX trips_route ON trips (pu_location_id, do_location_id)"
        " INCLUDE (pickup_at, dropoff_at, fare_amount, total_amount)",
    ),
    (
        "mv_daily",
        """
     CREATE MATERIALIZED VIEW mv_daily AS
     SELECT pickup_at::date AS day,
            coalesce(vendor_id, 0) AS vendor_id,
            pu_location_id,
            coalesce(payment_type, 0) AS payment_type,
            count(*) AS trips,
            sum(fare_amount) AS fare,
            sum(tip_amount) AS tips,
            sum(total_amount) AS revenue,
            sum(trip_distance) AS distance
     FROM trips
     GROUP BY 1, 2, 3, 4""",
    ),
    (
        "mv_hourly_zone",
        """
     CREATE MATERIALIZED VIEW mv_hourly_zone AS
     SELECT pickup_at::date AS day, extract(hour FROM pickup_at)::int AS hour,
            pu_location_id, count(*) AS trips
     FROM trips GROUP BY 1, 2, 3""",
    ),
    (
        "mv_hourly_zone_unique",
        "CREATE UNIQUE INDEX mv_hourly_zone_key ON mv_hourly_zone (pu_location_id, day, hour)",
    ),
    # Exact percentiles without sorting millions of rows: one row per distinct value with its
    # count. A running sum over the counts finds the same value percentile_disc would.
    (
        "mv_duration_hist",
        """
     CREATE MATERIALIZED VIEW mv_duration_hist AS
     SELECT date_trunc('month', t.pickup_at)::date AS month, z.borough,
            extract(epoch FROM t.dropoff_at - t.pickup_at)::int AS duration_s, count(*) AS n
     FROM trips t JOIN zones z ON z.location_id = t.pu_location_id
     GROUP BY 1, 2, 3""",
    ),
    (
        "mv_duration_hist_unique",
        "CREATE UNIQUE INDEX mv_duration_hist_key ON mv_duration_hist (month, borough, duration_s)",
    ),
    (
        "mv_tip_hist",
        """
     CREATE MATERIALIZED VIEW mv_tip_hist AS
     SELECT date_trunc('month', pickup_at)::date AS month,
            round(tip_amount * 100 / fare_amount, 2) AS tip_pct, count(*) AS n
     FROM trips WHERE payment_type = 1 AND fare_amount > 0
     GROUP BY 1, 2""",
    ),
    ("mv_tip_hist_unique", "CREATE UNIQUE INDEX mv_tip_hist_key ON mv_tip_hist (month, tip_pct)"),
    (
        "mv_daily_unique",
        # Unique index: makes REFRESH MATERIALIZED VIEW CONCURRENTLY possible (readers aren't
        # blocked while it refreshes) and serves day-range lookups.
        "CREATE UNIQUE INDEX mv_daily_key ON mv_daily"
        " (day, vendor_id, pu_location_id, payment_type)",
    ),
]


def create_zones(conn: psycopg.Connection, data_dir: Path) -> None:
    conn.execute("DROP TABLE IF EXISTS zones CASCADE")
    conn.execute(
        "CREATE TABLE zones (location_id smallint PRIMARY KEY, borough text,"
        " zone text, service_zone text)"
    )
    with (
        conn.cursor() as cur,
        cur.copy("COPY zones FROM STDIN WITH (FORMAT csv, HEADER true)") as copy,
    ):
        copy.write((data_dir / "taxi_zone_lookup.csv").read_bytes())


def create_baseline(conn: psycopg.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS trips_heap")
    conn.execute(f"CREATE TABLE trips_heap ({COLUMNS_SQL})")


def create_tuned(conn: psycopg.Connection) -> None:
    for mv in ("mv_daily", "mv_hourly_zone", "mv_duration_hist", "mv_tip_hist"):
        conn.execute(f"DROP MATERIALIZED VIEW IF EXISTS {mv}")
    conn.execute("DROP TABLE IF EXISTS trips CASCADE")
    conn.execute(f"CREATE TABLE trips ({COLUMNS_SQL}) PARTITION BY RANGE (pickup_at)")
    for lo, hi in itertools.pairwise(MONTHS):
        conn.execute(
            f"CREATE TABLE trips_{lo[:7].replace('-', '_')} PARTITION OF trips"
            f" FOR VALUES FROM ('{lo}') TO ('{hi}')"
        )
    # Rows dated outside 2023 (bad device clocks: 2002, 2008, 2024...) still have a home.
    conn.execute("CREATE TABLE trips_default PARTITION OF trips DEFAULT")


def copy_files(
    conn: psycopg.Connection,
    table: str,
    files: list[Path],
    sort_by_pickup: bool,
    limit: int | None = None,
) -> dict[str, Any]:
    """COPY every month into `table`; Arrow writes CSV in C, so Python never touches rows."""
    t0 = time.time()
    rows = 0
    opts = pacsv.WriteOptions(include_header=False)
    cols = ", ".join(source.COLUMNS)
    for f in files:
        with (
            conn.cursor() as cur,
            cur.copy(f"COPY {table} ({cols}) FROM STDIN WITH (FORMAT csv)") as copy,
        ):
            for batch in source.batches(f, sort_by_pickup=sort_by_pickup):
                if limit is not None:
                    batch = batch.slice(0, max(0, limit - rows))
                    if batch.num_rows == 0:
                        break
                buf = io.BytesIO()
                pacsv.write_csv(batch, buf, opts)
                copy.write(buf.getvalue())
                rows += batch.num_rows
        log.info("%s: loaded %s (%s rows so far)", table, f.name, f"{rows:,}")
        if limit is not None and rows >= limit:
            break
    load_s = time.time() - t0
    t1 = time.time()
    conn.execute(f"VACUUM (ANALYZE) {table}")
    return {
        "rows": rows,
        "load_s": round(load_s, 1),
        "rows_per_s": round(rows / load_s),
        "vacuum_analyze_s": round(time.time() - t1, 1),
    }


def tune(conn: psycopg.Connection) -> list[dict[str, Any]]:
    steps = []
    for name, ddl in TUNING:
        t0 = time.time()
        conn.execute(ddl)
        steps.append({"step": name, "seconds": round(time.time() - t0, 1)})
        log.info("tuning step %s: %.1fs", name, time.time() - t0)
    conn.execute("VACUUM (ANALYZE) trips")
    for mv in MVS:
        conn.execute(f"VACUUM (ANALYZE) {mv}")
    return steps


MVS = ["mv_daily", "mv_hourly_zone", "mv_duration_hist", "mv_tip_hist"]


def refresh_mvs(conn: psycopg.Connection) -> dict[str, float]:
    """REFRESH ... CONCURRENTLY: readers keep using the old contents until the new ones are
    ready (needs the unique indexes above). The cost of freshness, reported in RESULTS.md."""
    out = {}
    for mv in MVS:
        t0 = time.time()
        conn.execute(f"REFRESH MATERIALIZED VIEW CONCURRENTLY {mv}")
        out[mv] = round(time.time() - t0, 1)
    return out


def sizes(conn: psycopg.Connection) -> dict[str, str]:
    q = """
    SELECT 'trips_heap', pg_size_pretty(pg_total_relation_size('trips_heap'))
      WHERE to_regclass('trips_heap') IS NOT NULL
    UNION ALL
    SELECT 'trips (all partitions, data)', pg_size_pretty(sum(pg_relation_size(inhrelid)))
      FROM pg_inherits WHERE inhparent = to_regclass('trips')
    UNION ALL
    SELECT c.relname, pg_size_pretty(sum(pg_relation_size(i.inhrelid)))
      FROM pg_class c JOIN pg_inherits i ON i.inhparent = c.oid
     WHERE c.relkind = 'I' GROUP BY c.relname
    UNION ALL
    SELECT c.relname, pg_size_pretty(pg_total_relation_size(c.oid))
      FROM pg_class c WHERE c.relkind = 'm'
    """
    return dict(conn.execute(q).fetchall())


def pickup_correlation(conn: psycopg.Connection, table_pattern: str) -> float | None:
    """pg_stats correlation of pickup_at with physical order (1.0 = perfectly sorted). BRIN is
    only useful when this is near 1."""
    rows = conn.execute(
        "SELECT avg(correlation) FROM pg_stats WHERE attname = 'pickup_at' AND tablename LIKE %s",
        (table_pattern,),
    ).fetchone()
    return round(float(rows[0]), 4) if rows and rows[0] is not None else None
