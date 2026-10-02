"""MySQL 8.4 with the closest equivalent of the tuned PostgreSQL design.

| PostgreSQL (tuned)                       | MySQL 8.4                                          |
|------------------------------------------|----------------------------------------------------|
| RANGE partitions by month + DEFAULT      | RANGE COLUMNS partitions by month + p_old/p_future |
| BRIN (pickup_at)                         | no BRIN: a B-tree on pickup_at instead             |
| B-tree (pu_location_id, pickup_at)       | same                                               |
| B-tree (pu, do) INCLUDE (4 columns)      | no INCLUDE: all 6 columns as key columns           |
| materialized views (4)                   | no MVs: plain summary tables built the same way    |
| percentile_disc                          | none: ROW_NUMBER() over the sorted values          |
| parallel query                           | none: one thread per query                         |

InnoDB clusters every table on its primary key. The trips table has no natural key, so InnoDB
adds a hidden 6-byte row id, and secondary-index lookups go through it.
"""

from __future__ import annotations

import io
import logging
import time
from pathlib import Path
from typing import Any

import pyarrow.csv as pacsv

from . import source
from .config import Settings

log = logging.getLogger(__name__)

TRIPS = """
CREATE TABLE trips (
    vendor_id SMALLINT,
    pickup_at DATETIME NOT NULL,
    dropoff_at DATETIME NOT NULL,
    passenger_count SMALLINT,
    trip_distance DECIMAL(9,2),
    ratecode_id SMALLINT,
    store_and_fwd_flag CHAR(1),
    pu_location_id SMALLINT NOT NULL,
    do_location_id SMALLINT NOT NULL,
    payment_type SMALLINT,
    fare_amount DECIMAL(10,2),
    extra DECIMAL(10,2),
    mta_tax DECIMAL(10,2),
    tip_amount DECIMAL(10,2),
    tolls_amount DECIMAL(10,2),
    improvement_surcharge DECIMAL(10,2),
    total_amount DECIMAL(10,2),
    congestion_surcharge DECIMAL(10,2),
    airport_fee DECIMAL(10,2)
) ENGINE=InnoDB
PARTITION BY RANGE COLUMNS (pickup_at) (
    PARTITION p_old VALUES LESS THAN ('2023-01-01'),
{months}
    PARTITION p_future VALUES LESS THAN (MAXVALUE)
)"""

TUNING = [
    ("btree_pickup", "CREATE INDEX trips_pickup ON trips (pickup_at)"),
    ("btree_zone_time", "CREATE INDEX trips_pu_pickup ON trips (pu_location_id, pickup_at)"),
    (
        "btree_route_wide",
        "CREATE INDEX trips_route ON trips (pu_location_id, do_location_id,"
        " pickup_at, dropoff_at, fare_amount, total_amount)",
    ),
    (
        "summary_daily",
        """
     CREATE TABLE mv_daily (PRIMARY KEY (day, vendor_id, pu_location_id, payment_type)) AS
     SELECT DATE(pickup_at) AS day, COALESCE(vendor_id, 0) AS vendor_id, pu_location_id,
            COALESCE(payment_type, 0) AS payment_type, COUNT(*) AS trips,
            SUM(fare_amount) AS fare, SUM(tip_amount) AS tips, SUM(total_amount) AS revenue,
            SUM(trip_distance) AS distance
     FROM trips GROUP BY 1, 2, 3, 4""",
    ),
    (
        "summary_hourly_zone",
        """
     CREATE TABLE mv_hourly_zone (PRIMARY KEY (pu_location_id, day, hour)) AS
     SELECT DATE(pickup_at) AS day, HOUR(pickup_at) AS hour, pu_location_id, COUNT(*) AS trips
     FROM trips GROUP BY 1, 2, 3""",
    ),
    (
        "summary_duration_hist",
        """
     CREATE TABLE mv_duration_hist (PRIMARY KEY (month, borough, duration_s)) AS
     SELECT CAST(DATE_FORMAT(t.pickup_at, '%Y-%m-01') AS DATE) AS month, z.borough,
            TIMESTAMPDIFF(SECOND, t.pickup_at, t.dropoff_at) AS duration_s, COUNT(*) AS n
     FROM trips t JOIN zones z ON z.location_id = t.pu_location_id GROUP BY 1, 2, 3""",
    ),
    (
        "summary_tip_hist",
        """
     CREATE TABLE mv_tip_hist (PRIMARY KEY (month, tip_pct)) AS
     SELECT CAST(DATE_FORMAT(pickup_at, '%Y-%m-01') AS DATE) AS month,
            ROUND(tip_amount * 100 / fare_amount, 2) AS tip_pct, COUNT(*) AS n
     FROM trips WHERE payment_type = 1 AND fare_amount > 0 GROUP BY 1, 2""",
    ),
]


def create(s: Settings) -> None:
    conn = s.mysql()
    with conn.cursor() as cur:
        for t in (
            "mv_daily",
            "mv_hourly_zone",
            "mv_duration_hist",
            "mv_tip_hist",
            "trips",
            "zones",
        ):
            cur.execute(f"DROP TABLE IF EXISTS {t}")
        months = "\n".join(
            f"    PARTITION p2023_{m:02d} VALUES LESS THAN ('{'2024' if m == 12 else '2023'}-"
            f"{1 if m == 12 else m + 1:02d}-01'),"
            for m in range(1, 13)
        )
        cur.execute(TRIPS.format(months=months))
        cur.execute(
            "CREATE TABLE zones (location_id SMALLINT PRIMARY KEY, borough VARCHAR(32),"
            " zone VARCHAR(64), service_zone VARCHAR(32))"
        )
    conn.close()


def load(s: Settings, csv_dir: Path, limit: int | None = None) -> dict[str, Any]:
    """Arrow writes one CSV per month (nulls as empty fields); LOAD DATA turns empty back into
    NULL with NULLIF, because MySQL would otherwise read '' as 0 in numeric columns."""
    csv_dir.mkdir(parents=True, exist_ok=True)
    conn = s.mysql(local_infile=True)
    cols = source.COLUMNS
    sets = ", ".join(f"{c} = NULLIF(@{c}, '')" for c in cols)
    t0 = time.time()
    rows = 0
    with conn.cursor() as cur:
        cur.execute(
            f"LOAD DATA LOCAL INFILE '{(s.data_dir / 'taxi_zone_lookup.csv').resolve()}'"
            " INTO TABLE zones FIELDS TERMINATED BY ',' OPTIONALLY ENCLOSED BY '\"'"
            # The TLC file has Windows line endings. With the default '\n' every quoted last
            # field ends in '"\r', the quote never closes, and every other row is swallowed:
            # 133 of 265 zones loaded, silently. The result-equality check caught it.
            " LINES TERMINATED BY '\r\n' IGNORE 1 LINES"
        )
        cur.execute("SELECT count(*) FROM zones")
        zones = cur.fetchone()[0]  # type: ignore[index]
        expected = sum(1 for _ in (s.data_dir / "taxi_zone_lookup.csv").open()) - 1
        if zones != expected:
            raise RuntimeError(f"zones: loaded {zones} rows, file has {expected}")
        for f in source.files(s.data_dir):
            t = source.read_month(f)
            if limit is not None:
                t = t.slice(0, max(0, limit - rows))
            path = csv_dir / (f.stem + ".csv")
            buf = io.BytesIO()
            pacsv.write_csv(t, buf, pacsv.WriteOptions(include_header=False))
            path.write_bytes(buf.getvalue())
            cur.execute(
                f"LOAD DATA LOCAL INFILE '{path.resolve()}' INTO TABLE trips"
                " FIELDS TERMINATED BY ',' OPTIONALLY ENCLOSED BY '\"'"
                f" ({', '.join('@' + c for c in cols)}) SET {sets}"
            )
            rows += t.num_rows
            path.unlink()
            log.info("mysql: loaded %s (%s rows)", f.name, f"{rows:,}")
            if limit is not None and rows >= limit:
                break
        load_s = time.time() - t0
        t1 = time.time()
        cur.execute("ANALYZE TABLE trips, zones")
    conn.close()
    return {
        "rows": rows,
        "load_s": round(load_s, 1),
        "rows_per_s": round(rows / load_s),
        "analyze_s": round(time.time() - t1, 1),
    }


def tune(s: Settings) -> list[dict[str, Any]]:
    conn = s.mysql()
    steps = []
    with conn.cursor() as cur:
        for name, ddl in TUNING:
            t0 = time.time()
            cur.execute(ddl)
            steps.append({"step": name, "seconds": round(time.time() - t0, 1)})
            log.info("mysql tuning %s: %.1fs", name, time.time() - t0)
        cur.execute("ANALYZE TABLE trips, mv_daily, mv_hourly_zone, mv_duration_hist, mv_tip_hist")
    conn.close()
    return steps


def sizes(s: Settings) -> dict[str, str]:
    conn = s.mysql()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name, CONCAT(ROUND(data_length / 1048576), ' MB data, ',"
            " ROUND(index_length / 1048576), ' MB indexes') FROM information_schema.tables"
            " WHERE table_schema = %s",
            (s.mysql_db,),
        )
        out = {r[0]: r[1] for r in cur.fetchall()}
    conn.close()
    return out
