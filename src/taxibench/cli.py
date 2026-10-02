"""Command line: taxibench <command>

  load-pg [--limit N]        zones, then the baseline (trips_heap) and the tuned design (trips)
  bench --design D           D = pg-baseline | pg-tuned | mysql; 5 timed runs per query
  compare                    check every design returned the same results; print the table
  refresh                    REFRESH MATERIALIZED VIEW CONCURRENTLY, timed
  drop-baseline              free the baseline's ~4.5 GB (to make room for MySQL)
  load-mysql [--limit N]     the MySQL 8 equivalent (needs: docker compose --profile mysql up)

Results go to out/*.json; plans to docs/plans/<design>/.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

from . import bench, mysql, pg, source
from .config import Settings
from .queries import BY_ID, SUITE

OUT = Path("out")
DESIGNS = ["pg-baseline", "pg-tuned", "mysql"]


def _save(name: str, data: Any) -> Path:
    OUT.mkdir(exist_ok=True)
    p = OUT / f"{name}.json"
    p.write_text(json.dumps(data, indent=2, default=str))
    return p


def compare(results: dict[str, dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    base = results["pg-baseline"]["queries"]
    rows, ok = [], True
    for q in SUITE:
        r: dict[str, Any] = {"query": q.id, "title": q.title, "exercises": q.exercises}
        for d in DESIGNS:
            if d in results and q.id in results[d]["queries"]:
                r[d] = results[d]["queries"][q.id]["median_ms"]
        if "pg-tuned" in results:
            same = results["pg-tuned"]["queries"][q.id]["fingerprint"] == base[q.id]["fingerprint"]
            r["tuned_same_result"] = same
            ok &= same
            r["speedup"] = round(base[q.id]["median_ms"] / max(r["pg-tuned"], 0.01), 1)
        if "mysql" in results:
            same = bench.close_enough(
                results["mysql"]["queries"][q.id]["result"], base[q.id]["result"]
            )
            r["mysql_same_result"] = same
            ok &= same
        rows.append(r)
    return rows, ok


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    p = argparse.ArgumentParser(
        prog="taxibench", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    lp = sub.add_parser("load-pg")
    lp.add_argument("--limit", type=int)
    b = sub.add_parser("bench")
    b.add_argument("--design", choices=DESIGNS, required=True)
    b.add_argument("--runs", type=int, default=5)
    b.add_argument("--query", action="append", help="only these query ids")
    sub.add_parser("compare")
    sub.add_parser("refresh")
    sub.add_parser("drop-baseline")
    lm = sub.add_parser("load-mysql")
    lm.add_argument("--limit", type=int)
    a = p.parse_args(argv)
    s = Settings.from_env()

    if a.cmd == "load-pg":
        conn = s.pg()
        files = source.files(s.data_dir)
        pg.create_zones(conn, s.data_dir)
        pg.create_baseline(conn)
        baseline = pg.copy_files(conn, "trips_heap", files, sort_by_pickup=False, limit=a.limit)
        baseline["correlation"] = pg.pickup_correlation(conn, "trips_heap")
        pg.create_tuned(conn)
        tuned = pg.copy_files(conn, "trips", files, sort_by_pickup=True, limit=a.limit)
        tuned["tuning"] = pg.tune(conn)
        tuned["correlation"] = pg.pickup_correlation(conn, "trips_2023%")
        out: dict[str, Any] = {"baseline": baseline, "tuned": tuned, "sizes": pg.sizes(conn)}
        print(json.dumps(out, indent=1), "\n->", _save("load-pg", out))
    elif a.cmd == "bench":
        qs = [BY_ID[q] for q in a.query] if a.query else None
        res = bench.run_suite(s, a.design, a.runs, qs)
        print(
            f"suite median {res['suite_median_ms']} ms, total {res['suite_total_ms']} ms",
            "->",
            _save(f"bench-{a.design}", res),
        )
    elif a.cmd == "compare":
        results = {
            d: json.loads((OUT / f"bench-{d}.json").read_text())
            for d in DESIGNS
            if (OUT / f"bench-{d}.json").exists()
        }
        rows, ok = compare(results)
        print(f"{'query':32s} {'baseline':>10s} {'tuned':>9s} {'speedup':>8s} {'mysql':>9s}  same")
        for r in rows:
            same = r.get("tuned_same_result", "-"), r.get("mysql_same_result", "-")
            print(
                f"{r['query']:32s} {r.get('pg-baseline', '-'):>10} {r.get('pg-tuned', '-'):>9}"
                f" {r.get('speedup', '-'):>7}x {r.get('mysql', '-'):>9}  {same}"
            )
        summary = {
            d: {
                "suite_median_ms": results[d]["suite_median_ms"],
                "suite_total_ms": results[d]["suite_total_ms"],
            }
            for d in results
        }
        print(json.dumps(summary))
        _save("compare", {"rows": rows, "summary": summary, "all_results_match": ok})
        return 0 if ok else 1
    elif a.cmd == "refresh":
        print(json.dumps(pg.refresh_mvs(s.pg())))
    elif a.cmd == "drop-baseline":
        s.pg().execute("DROP TABLE IF EXISTS trips_heap")
        print("dropped trips_heap")
    elif a.cmd == "load-mysql":
        mysql.create(s)
        out = {
            "load": mysql.load(s, OUT / "csv", a.limit),
            "tuning": mysql.tune(s),
            "sizes": mysql.sizes(s),
        }
        print(json.dumps(out, indent=1), "\n->", _save("load-mysql", out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
