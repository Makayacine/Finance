"""The whole chain on a laptop with no AWS account: fetch, load, engine.

Two local folders stand in for the two buckets, with every file at its S3 key:

    _localrun/raw-bucket/raw/<source>/date=<D>/<file>       what the Lambda writes
    _localrun/lake-bucket/<table>/...                       what the loader and the engine write

The fetch step is the committed sample by default: ``data/sample`` holds October 2026 and the
two crisis dates of the truth check, gzipped, at the same keys. ``--fetch`` runs the Lambda's
own fetcher for each business day from ``--start`` to ``--end`` instead, one FedInvest POST a
day with a pause between them. Then the Glue job's ``run`` loads the raw zone, and the ECS
engine builds the curves, the fit errors and the risk.

    python local-development/run_local.py
    python local-development/run_local.py --fetch --start 2026-11-02 --end 2026-11-03
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import logging
import shutil
import sys
import warnings
from pathlib import Path

warnings.simplefilter("ignore", SyntaxWarning)  # FIA.py compares a string with 'is not'

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
for p in (PROJECT, PROJECT / "lambda-functions", PROJECT / "glue-pythonshell", PROJECT / "ecs"):
    sys.path.insert(0, str(p))

import fetch  # noqa: E402
import load  # noqa: E402
from bond_analytics import dates  # noqa: E402

SAMPLE = PROJECT / "data" / "sample"
RUN = PROJECT / "_localrun"


def stage_sample(raw: load.FolderStore) -> int:
    """Copy the committed sample into the raw bucket folder, unzipped, at the Lambda's keys."""
    n = 0
    for path in sorted(SAMPLE.rglob("*.gz")):
        k = path.relative_to(SAMPLE).with_suffix("").as_posix()
        raw.put(k, gzip.decompress(path.read_bytes()))
        n += 1
    return n


def stage_fetch(raw: load.FolderStore, start: dt.date, end: dt.date) -> dict:
    days = []
    d = start
    while d <= end:
        if dates.is_business_day(d):
            days.append(d)
        d += dt.timedelta(days=1)
    status = fetch.backfill(days, raw.put, sources=("fedinvest", "nyfed"))
    status[end] = fetch.fetch_all(end, raw.put, sources=("gsw", "auctions"))
    return status


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--fetch", action="store_true", help="fetch from the sources instead of staging the sample")
    ap.add_argument("--start", type=dt.date.fromisoformat)
    ap.add_argument("--end", type=dt.date.fromisoformat)
    ap.add_argument("--keep", action="store_true", help="keep what is already in _localrun/")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")

    if not args.keep and RUN.exists():
        for sub in ("raw-bucket", "lake-bucket"):
            shutil.rmtree(RUN / sub, ignore_errors=True)
    raw, lake = load.FolderStore(RUN / "raw-bucket"), load.FolderStore(RUN / "lake-bucket")

    if args.fetch:
        if not (args.start and args.end):
            ap.error("--fetch needs --start and --end")
        status = stage_fetch(raw, args.start, args.end)
        failed = {d: s for d, by in status.items() for src, s in by.items() if s.startswith("failed")}
        print(f"fetch: {len(status)} dates, {len(failed)} with a failed source")
    else:
        print(f"fetch: staged {stage_sample(raw)} sample objects from data/sample")

    status = load.run(raw, lake, "full")
    print("load: " + "; ".join(f"{s} {v}" for s, v in status.items()))
    prices = lake.list("prices/")
    print(f"load: {len(prices)} price dates in the lake, watermarks {sorted((s, m['loaded_through']) for s, m in load.read_watermarks(lake).items())}")

    try:
        import app
    except ImportError:
        return
    summary = app.run(lake, load_type="full")
    print("engine: " + "; ".join(f"{k} {v}" for k, v in summary.items()))


if __name__ == "__main__":
    main()
