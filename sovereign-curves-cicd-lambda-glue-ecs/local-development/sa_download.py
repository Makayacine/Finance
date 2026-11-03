"""Download the South African sources and write the parsed rows the project commits.

The National Treasury permits downloading and copying its material for educational and
non-commercial use, provided it is not modified or misrepresented, the Treasury is acknowledged
and copyright notices stay intact. So its spreadsheets and PDFs are not committed: this script
fetches them with the Lambda's own fetchers, parses them with the loader's own parsers, and
writes the rows, every one carrying ``Source: National Treasury of the Republic of South
Africa``, to

    data/sa/nt_auctions.csv     every fixed-rate bond auction since April 2010, flags and all
    data/sa/nt_bonds.csv        each auctioned bond's coupon, cycle and redemption, from the schedules

The OECD and IMF series allow redistribution with their citations, so those two files go into
``data/sample/raw/`` gzipped, at the keys the Lambda writes them to, like the US sample.

One request at a time, two seconds apart: the 16 auction files, one debt schedule a year (31
March) from 2011 and the latest, and the two series. ``--raw`` also keeps the Treasury's files in
a local raw-bucket folder, gitignored, so ``run_local.py --sa-raw`` can load them through the
loader itself; ``--from-raw`` parses such a folder again without fetching anything.

    python local-development/sa_download.py --raw _localrun/sa-raw
    python local-development/sa_download.py --from-raw _localrun/sa-raw
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
for p in (PROJECT, PROJECT / "lambda-functions", PROJECT / "glue-pythonshell"):
    sys.path.insert(0, str(p))

import fetch  # noqa: E402
from bond_analytics import nt_auctions, nt_schedule, schemas  # noqa: E402

DATA = PROJECT / "data" / "sa"
SAMPLE_RAW = PROJECT / "data" / "sample" / "raw"
PAUSE = 2.0


def schedules(pause: float, keep=None) -> dict:
    listed = fetch.nt_schedules_listed()
    wanted = sorted({d for d in listed if d.month == 3 and d.day == 31 and d.year >= 2011} | {max(listed)})
    parsed = []
    for i, d in enumerate(wanted):
        if i:
            time.sleep(pause)
        body = fetch.fetch_nt_schedule_as_at(listed[d])
        if keep:
            keep(fetch.raw_key("nt_schedule", d, "schedule.pdf"), body)
        parsed.append(nt_schedule.parse_pdf(body, listed[d]))
    print(f"schedules: {len(wanted)}, {wanted[0]} to {wanted[-1]}")
    return nt_schedule.merge(parsed)


def auctions(bonds: dict, today: dt.date, pause: float, keep=None) -> list:
    rows = []
    files = fetch.fetch_nt_auction_history(pause_seconds=pause)
    for name, body in sorted(files.items()):
        if keep:
            keep(fetch.raw_key("nt_auctions", today, name), body)
        rows.extend(nt_auctions.parse_file(body, name))
    print(f"auction files: {len(files)}, {len(rows)} bond columns read")
    return nt_auctions.flag_neighbours(nt_auctions.deduplicate(nt_auctions.flag(rows, bonds)))


def from_raw(folder: Path) -> tuple:
    """(bonds, auction rows) from a raw-bucket folder an earlier ``--raw`` run kept."""
    import load

    store = load.FolderStore(folder)
    bonds = nt_schedule.merge(nt_schedule.parse_pdf(store.get(k)) for k in store.list("raw/nt_schedule/"))
    rows = []
    for k in sorted(store.list("raw/nt_auctions/")):
        rows.extend(nt_auctions.parse_file(store.get(k), k.rsplit("/", 1)[1]))
    return bonds, nt_auctions.flag_neighbours(nt_auctions.deduplicate(nt_auctions.flag(rows, bonds)))


def write_rows(bonds: dict, rows: list) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    bond_rows = [{**b, "redemptions": ",".join(d.isoformat() for d in b["redemptions"]), "legs": float(b["legs"])}
                 for _, b in sorted(bonds.items())]
    (DATA / "nt_bonds.csv").write_text(schemas.to_csv("sa_bonds", bond_rows), encoding="utf-8")
    (DATA / "nt_auctions.csv").write_text(schemas.to_csv("sa_auctions", rows), encoding="utf-8")
    flagged = sum(1 for r in rows if r["quality_flags"])
    out = sum(1 for r in rows if not nt_auctions.usable(r))
    print(f"wrote {len(bond_rows)} bonds and {len(rows)} auctions ({flagged} flagged, {out} kept out of the curve) to data/sa/")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--raw", type=Path, help="also keep the Treasury's files in this raw-bucket folder")
    ap.add_argument("--from-raw", type=Path, help="parse the files an earlier --raw run kept; fetch nothing")
    ap.add_argument("--pause", type=float, default=PAUSE)
    args = ap.parse_args(argv)
    today = dt.datetime.now(dt.timezone.utc).date()

    if args.from_raw:
        write_rows(*from_raw(args.from_raw))
        return

    keep = None
    if args.raw:
        import load

        store = load.FolderStore(args.raw)
        keep = store.put

    bonds = schedules(args.pause, keep)
    time.sleep(args.pause)
    write_rows(bonds, auctions(bonds, today, args.pause, keep))

    for source, name, fn in (("oecd", "irlt.csv", fetch.fetch_oecd), ("imf", "INTGSBZAM193N.csv", fetch.fetch_imf)):
        time.sleep(args.pause)
        body = fn(today)
        path = SAMPLE_RAW / source / f"date={today.isoformat()}" / (name + ".gz")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(gzip.compress(body, mtime=0))
        if keep:
            keep(fetch.raw_key(source, today, name), body)
        print(f"{source}: {len(body):,} bytes to {path.relative_to(PROJECT).as_posix()}")


if __name__ == "__main__":
    main()
