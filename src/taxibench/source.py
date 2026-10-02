"""Reads the TLC Parquet files into one consistent Arrow schema.

The monthly files disagree with each other: January stores VendorID as int64 and
passenger_count as double, later months use int32/int64, and the airport fee column is
`airport_fee` in January but `Airport_fee` from February on. Everything is renamed to snake_case
and cast to one target schema here, so the loaders never see the differences.

Money and distances become decimal(10,2)/decimal(9,2): exact, so sums agree to the cent across
PostgreSQL, MySQL, the baseline and the tuned schema, whatever order rows are added in.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

RENAME = {
    "VendorID": "vendor_id",
    "tpep_pickup_datetime": "pickup_at",
    "tpep_dropoff_datetime": "dropoff_at",
    "passenger_count": "passenger_count",
    "trip_distance": "trip_distance",
    "RatecodeID": "ratecode_id",
    "store_and_fwd_flag": "store_and_fwd_flag",
    "PULocationID": "pu_location_id",
    "DOLocationID": "do_location_id",
    "payment_type": "payment_type",
    "fare_amount": "fare_amount",
    "extra": "extra",
    "mta_tax": "mta_tax",
    "tip_amount": "tip_amount",
    "tolls_amount": "tolls_amount",
    "improvement_surcharge": "improvement_surcharge",
    "total_amount": "total_amount",
    "congestion_surcharge": "congestion_surcharge",
    "airport_fee": "airport_fee",
    "Airport_fee": "airport_fee",
}

MONEY = pa.decimal128(10, 2)
SCHEMA = pa.schema(
    [
        ("vendor_id", pa.int16()),
        ("pickup_at", pa.timestamp("us")),
        ("dropoff_at", pa.timestamp("us")),
        ("passenger_count", pa.int16()),
        ("trip_distance", pa.decimal128(9, 2)),
        ("ratecode_id", pa.int16()),
        ("store_and_fwd_flag", pa.string()),
        ("pu_location_id", pa.int16()),
        ("do_location_id", pa.int16()),
        ("payment_type", pa.int16()),
        ("fare_amount", MONEY),
        ("extra", MONEY),
        ("mta_tax", MONEY),
        ("tip_amount", MONEY),
        ("tolls_amount", MONEY),
        ("improvement_surcharge", MONEY),
        ("total_amount", MONEY),
        ("congestion_surcharge", MONEY),
        ("airport_fee", MONEY),
    ]
)
COLUMNS = SCHEMA.names


def files(data_dir: Path) -> list[Path]:
    found = sorted(data_dir.glob("yellow_tripdata_*.parquet"))
    if not found:
        raise FileNotFoundError(f"no yellow_tripdata_*.parquet in {data_dir} (run `make data`)")
    return found


def _normalise(t: pa.Table) -> pa.Table:
    t = t.rename_columns([RENAME[n] for n in t.column_names])
    cols = []
    for f in SCHEMA:
        c = t.column(f.name)
        if pa.types.is_decimal(f.type):
            # Round the float first: 12.1 is stored as 12.0999999..., and a decimal cast would
            # otherwise refuse the lost precision.
            c = pc.round(c.cast(pa.float64()), 2)
        cols.append(c.cast(f.type))
    return pa.Table.from_arrays(cols, schema=SCHEMA)


def read_month(path: Path, sort_by_pickup: bool = False) -> pa.Table:
    t = _normalise(pq.read_table(path))
    if sort_by_pickup:
        t = t.sort_by("pickup_at")
    return t


def batches(
    path: Path, rows: int = 250_000, sort_by_pickup: bool = False
) -> Iterator[pa.RecordBatch]:
    yield from read_month(path, sort_by_pickup).to_batches(max_chunksize=rows)
