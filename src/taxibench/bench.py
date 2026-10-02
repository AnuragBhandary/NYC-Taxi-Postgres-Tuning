"""Runs the suite against one design and records timings, plans and result fingerprints.

Designs: pg-baseline, pg-tuned, mysql.

Per query: one warm-up run (the timings are warm-cache: the question is what the *design*
costs, not how fast the disk is), then N timed runs; the reported figure is the median. Then
EXPLAIN ANALYZE once more, saved to docs/plans/<design>/<query>.txt, so every number in
RESULTS.md links to the plan that produced it.

Results are normalised and fingerprinted. PostgreSQL baseline and tuned must match exactly; MySQL
must match within 0.01 on decimals (its DECIMAL division keeps 4 extra digits where PostgreSQL's
numeric keeps more, so a value exactly on a rounding boundary can differ by one cent).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import statistics
import time
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

from .config import Settings
from .queries import SUITE, Query

PLANS = Path("docs/plans")


def norm_value(v: Any) -> Any:
    if isinstance(v, dt.datetime):
        return v.isoformat(sep=" ")
    if isinstance(v, dt.date):
        return v.isoformat()
    if isinstance(v, Decimal | float):
        return round(float(v), 4)
    return v


def normalise(rows: list[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
    return [tuple(norm_value(v) for v in r) for r in rows]


def fingerprint(rows: list[tuple[Any, ...]]) -> str:
    return hashlib.sha256(json.dumps(normalise(rows), default=str).encode()).hexdigest()[:16]


def close_enough(a: list[tuple[Any, ...]], b: list[tuple[Any, ...]], tol: float = 0.0101) -> bool:
    a, b = normalise(a), normalise(b)
    if len(a) != len(b):
        return False
    for ra, rb in zip(a, b, strict=True):
        if len(ra) != len(rb):
            return False
        for x, y in zip(ra, rb, strict=True):
            if isinstance(x, float) or isinstance(y, float):
                if x is None or y is None or abs(float(x) - float(y)) > tol:
                    return False
            elif str(x) != str(y):
                return False
    return True


def _runner(
    s: Settings, design: str
) -> tuple[Callable[[str], list[tuple[Any, ...]]], Callable[[str], str], Callable[[], None]]:
    if design == "mysql":
        my = s.mysql()

        def run_my(sql: str) -> list[tuple[Any, ...]]:
            with my.cursor() as cur:
                cur.execute(sql)
                return list(cur.fetchall())

        def explain_my(sql: str) -> str:
            with my.cursor() as cur:
                cur.execute("EXPLAIN ANALYZE " + sql)
                return "\n".join(r[0] for r in cur.fetchall())

        return run_my, explain_my, my.close
    pg = s.pg()

    def run_pg(sql: str) -> list[tuple[Any, ...]]:
        return pg.execute(sql).fetchall()

    def explain_pg(sql: str) -> str:
        rows = pg.execute("EXPLAIN (ANALYZE, BUFFERS, SETTINGS) " + sql).fetchall()
        return "\n".join(r[0] for r in rows)

    return run_pg, explain_pg, pg.close


def sql_for(q: Query, design: str) -> str:
    if design == "mysql":
        return q.mysql
    return q.pg("tuned" if design == "pg-tuned" else "baseline")


def run_suite(
    s: Settings,
    design: str,
    runs: int = 5,
    queries: list[Query] | None = None,
    save_plans: bool = True,
    progress: Callable[[str], None] = print,
) -> dict[str, Any]:
    run, explain, close = _runner(s, design)
    out: dict[str, Any] = {"design": design, "runs": runs, "queries": {}}
    try:
        for q in queries or SUITE:
            sql = sql_for(q, design)
            rows = run(sql)  # warm-up
            times = []
            for _ in range(runs):
                t0 = time.perf_counter()
                rows = run(sql)
                times.append((time.perf_counter() - t0) * 1000)
            plan = explain(sql)
            if save_plans:
                d = PLANS / design
                d.mkdir(parents=True, exist_ok=True)
                (d / f"{q.id}.txt").write_text(f"-- {q.title}\n{sql.strip()}\n\n{plan}\n")
            med = statistics.median(times)
            out["queries"][q.id] = {
                "median_ms": round(med, 1),
                "min_ms": round(min(times), 1),
                "max_ms": round(max(times), 1),
                "rows": len(rows),
                "fingerprint": fingerprint(rows),
                "result": normalise(rows),
            }
            progress(f"{design:12s} {q.id:32s} median {med:9.1f} ms  rows {len(rows)}")
    finally:
        close()
    meds = [v["median_ms"] for v in out["queries"].values()]
    out["suite_median_ms"] = round(statistics.median(meds), 1)
    out["suite_total_ms"] = round(sum(meds), 1)
    return out
