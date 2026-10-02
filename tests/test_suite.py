"""Correctness of the whole benchmark on a 36k-trip sample (3,000 per month, real Parquet):
both PostgreSQL designs and MySQL must return the same answer to all 12 questions, and the plans
must show the mechanisms the tuning relies on."""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import psycopg
import pymysql
import pytest

from taxibench import bench, cli, mysql, pg, source
from taxibench.config import Settings
from taxibench.queries import BY_ID, SUITE

SAMPLE = Path(__file__).parent / "fixtures" / "sample"
BASE = Settings.from_env()
TEST = replace(
    BASE, pg_dsn=BASE.pg_dsn.rsplit("/", 1)[0] + "/taxi_test", mysql_db="taxi_test", data_dir=SAMPLE
)


def test_monthly_files_normalise_to_one_schema() -> None:
    jan = source.read_month(
        SAMPLE / "yellow_tripdata_2023-01.parquet"
    )  # int64 / double / airport_fee
    feb = source.read_month(SAMPLE / "yellow_tripdata_2023-02.parquet")  # int32 / Airport_fee
    assert jan.schema == feb.schema == source.SCHEMA
    fares = jan.column("fare_amount").to_pylist()
    assert all(f is None or f == f.quantize(Decimal("0.01")) for f in fares)


def test_compare_helpers() -> None:
    a = [("2023-01-01", Decimal("10.00"), 3)]
    assert bench.close_enough(a, [("2023-01-01", Decimal("10.01"), 3)])
    assert not bench.close_enough(a, [("2023-01-01", Decimal("10.02"), 3)])
    assert not bench.close_enough(a, [("2023-01-01", Decimal("10.00"), 4)])
    assert bench.fingerprint(a) == bench.fingerprint([("2023-01-01", Decimal("10.000"), 3)])


@pytest.fixture(scope="module")
def pg_loaded() -> Iterator[Settings]:
    try:
        admin = psycopg.connect(BASE.pg_dsn, autocommit=True)
    except psycopg.OperationalError as e:  # pragma: no cover
        pytest.skip(f"postgres not reachable: {e}")
    admin.execute("DROP DATABASE IF EXISTS taxi_test WITH (FORCE)")
    admin.execute("CREATE DATABASE taxi_test")
    admin.close()
    conn = TEST.pg()
    files = source.files(SAMPLE)
    pg.create_zones(conn, SAMPLE)
    pg.create_baseline(conn)
    pg.copy_files(conn, "trips_heap", files, sort_by_pickup=False)
    pg.create_tuned(conn)
    pg.copy_files(conn, "trips", files, sort_by_pickup=True)
    pg.tune(conn)
    conn.close()
    yield TEST


@pytest.fixture(scope="module")
def results(pg_loaded: Settings) -> dict[str, dict]:
    quiet = lambda _m: None  # noqa: E731
    return {
        d: bench.run_suite(pg_loaded, d, runs=1, save_plans=False, progress=quiet)
        for d in ("pg-baseline", "pg-tuned")
    }


def test_sample_loaded_everywhere(pg_loaded: Settings) -> None:
    with pg_loaded.pg() as c:
        heap = c.execute("SELECT count(*) FROM trips_heap").fetchone()
        tuned = c.execute("SELECT count(*) FROM trips").fetchone()
        parts = c.execute(
            "SELECT count(*) FROM pg_inherits WHERE inhparent = 'trips'::regclass"
        ).fetchone()
    assert heap == tuned and heap is not None and heap[0] > 36_000
    assert parts == (13,)  # 12 months + DEFAULT


@pytest.mark.parametrize("qid", [q.id for q in SUITE])
def test_tuned_returns_the_same_answer(results: dict[str, dict], qid: str) -> None:
    base, tuned = results["pg-baseline"]["queries"][qid], results["pg-tuned"]["queries"][qid]
    assert base["rows"] > 0, "the sample should exercise every query"
    assert tuned["fingerprint"] == base["fingerprint"], (base["result"][:3], tuned["result"][:3])


def _plan(s: Settings, sql: str) -> str:
    with s.pg() as c:
        return "\n".join(r[0] for r in c.execute("EXPLAIN " + sql).fetchall())  # type: ignore[operator]


@pytest.mark.parametrize(
    ("qid", "partitions"),
    [
        ("q02_week_daily", {"trips_2023_03"}),
        ("q08_top3_dropoffs", {"trips_2023_06"}),
        ("q12_fare_outliers_thanksgiving", {"trips_2023_11"}),
    ],
)
def test_partition_pruning(pg_loaded: Settings, qid: str, partitions: set[str]) -> None:
    plan = _plan(pg_loaded, BY_ID[qid].pg("tuned"))
    scanned = {
        p
        for p in [f"trips_2023_{m:02d}" for m in range(1, 13)] + ["trips_default"]
        if f" {p} " in plan or f" {p}\n" in plan or plan.endswith(p)
    }
    assert scanned == partitions, plan


@pytest.mark.parametrize(
    "qid",
    [
        "q01_monthly_revenue",
        "q04_jfk_hourly_profile",
        "q05_duration_pct_by_borough",
        "q06_tip_pct_quartiles",
        "q07_rolling_7day",
        "q09_vendor_mom",
        "q03_top_zones_march",
    ],
)
def test_rollups_never_touch_the_trips_table(pg_loaded: Settings, qid: str) -> None:
    plan = _plan(pg_loaded, BY_ID[qid].pg("tuned"))
    assert "mv_" in plan and "trips_2023" not in plan, plan


def test_brin_and_covering_index_are_chosen_when_selective(pg_loaded: Settings) -> None:
    with pg_loaded.pg() as c:
        c.execute("SET enable_seqscan = off")  # the sample is tiny; force the access-path choice
        q11 = "\n".join(
            r[0]
            for r in c.execute("EXPLAIN " + BY_ID["q11_route_midtown_jfk"].pg("tuned")).fetchall()
        )  # type: ignore[operator]
        q02 = "\n".join(
            r[0] for r in c.execute("EXPLAIN " + BY_ID["q02_week_daily"].pg("tuned")).fetchall()
        )  # type: ignore[operator]
    assert "Index Only Scan" in q11 and "pu_location_id_do_location_id" in q11, q11
    assert "pickup_at_idx" in q02, q02  # BRIN (or the zone B-tree) instead of a full scan


def test_mv_refresh_concurrently(pg_loaded: Settings) -> None:
    timings = pg.refresh_mvs(pg_loaded.pg())
    assert set(timings) == set(pg.MVS)


@pytest.fixture(scope="module")
def mysql_loaded(tmp_path_factory: pytest.TempPathFactory) -> Settings:
    try:
        root = pymysql.connect(
            host=BASE.mysql_host,
            port=BASE.mysql_port,
            user="root",
            password=os.environ.get("MYSQL_ROOT_PASSWORD", "root"),
        )
    except pymysql.err.OperationalError as e:  # pragma: no cover
        pytest.skip(f"mysql not reachable ({e}); docker compose --profile mysql up -d")
    with root.cursor() as cur:
        cur.execute("DROP DATABASE IF EXISTS taxi_test")
        cur.execute("CREATE DATABASE taxi_test")
        cur.execute("GRANT ALL ON taxi_test.* TO 'taxi'@'%'")
    root.close()
    mysql.create(TEST)
    mysql.load(TEST, tmp_path_factory.mktemp("csv"))
    mysql.tune(TEST)
    return TEST


@pytest.mark.parametrize("qid", [q.id for q in SUITE])
def test_mysql_returns_the_same_answer(
    mysql_loaded: Settings, results: dict[str, dict], qid: str
) -> None:
    out = bench.run_suite(
        mysql_loaded,
        "mysql",
        runs=1,
        queries=[BY_ID[qid]],
        save_plans=False,
        progress=lambda _m: None,
    )
    assert bench.close_enough(
        out["queries"][qid]["result"], results["pg-baseline"]["queries"][qid]["result"]
    ), (out["queries"][qid]["result"][:3], results["pg-baseline"]["queries"][qid]["result"][:3])


def test_cli_compare(
    results: dict[str, dict], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "OUT", tmp_path)
    for d, r in results.items():
        (tmp_path / f"bench-{d}.json").write_text(__import__("json").dumps(r, default=str))
    assert cli.main(["compare"]) == 0


def test_cli_end_to_end(
    mysql_loaded: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every command, against the test databases. Runs last: it ends by dropping the baseline."""
    monkeypatch.setenv("TAXI_PG_DSN", TEST.pg_dsn)
    monkeypatch.setenv("TAXI_MYSQL_DB", TEST.mysql_db)
    monkeypatch.setenv("TAXI_DATA_DIR", str(SAMPLE))
    monkeypatch.setattr(cli, "OUT", tmp_path)
    monkeypatch.setattr(bench, "PLANS", tmp_path / "plans")
    assert cli.main(["load-pg"]) == 0
    for d in ("pg-baseline", "pg-tuned", "mysql"):
        assert cli.main(["bench", "--design", d, "--runs", "1"]) == 0
    assert (tmp_path / "plans" / "pg-tuned" / "q11_route_midtown_jfk.txt").exists()
    assert cli.main(["compare"]) == 0
    assert cli.main(["refresh"]) == 0
    assert cli.main(["load-mysql"]) == 0
    assert cli.main(["drop-baseline"]) == 0
