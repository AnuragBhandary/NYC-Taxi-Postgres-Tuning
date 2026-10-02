"""Cuts tests/fixtures/sample: 3,000 random trips from each monthly file, kept in the original
Parquet schema (so the January vs February schema differences stay in the test data), plus the
zone lookup. Also keeps every Midtown -> JFK trip in the sample for q11.

    uv run python tests/make_sample.py data tests/fixtures/sample
"""

from __future__ import annotations

import shutil
import sys
from datetime import datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq


def main(src: Path, dst: Path, n: int = 3000, seed: int = 7) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    import random

    rnd = random.Random(seed)
    for f in sorted(src.glob("yellow_tripdata_2023-*.parquet")):
        t = pq.read_table(f)
        idx = sorted(rnd.sample(range(t.num_rows), n))
        route = pc.and_(pc.equal(t["PULocationID"], 161), pc.equal(t["DOLocationID"], 132))
        route_idx = [i for i, v in enumerate(route.to_pylist()) if v][:40]
        extra: list[int] = []
        if f.name.endswith("2023-11.parquet"):
            # q12 needs a full day per zone for its mean + 3 sd test: Thanksgiving in two zones.
            day = pc.and_(
                pc.greater_equal(
                    t["tpep_pickup_datetime"], pa.scalar(datetime(2023, 11, 23), pa.timestamp("us"))
                ),
                pc.less(
                    t["tpep_pickup_datetime"], pa.scalar(datetime(2023, 11, 24), pa.timestamp("us"))
                ),
            )
            zone = pc.is_in(t["PULocationID"], pa.array([142, 162], pa.int32()))
            extra = [i for i, v in enumerate(pc.and_(day, zone).to_pylist()) if v]
        keep = sorted(set(idx) | set(route_idx) | set(extra))
        pq.write_table(t.take(keep), dst / f.name, compression="zstd")
        print(f.name, len(keep))
    shutil.copy(src / "taxi_zone_lookup.csv", dst / "taxi_zone_lookup.csv")


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
