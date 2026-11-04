"""Glue Python Shell job: the raw zone into the lake, as Parquet, with a watermark per source.

Started by the fetch Lambda with ``--load_type incremental --date_filter <date>``, or by hand
with ``--load_type full`` to rebuild. Python 3.9, the only Python Shell runtime, with the
``analytics`` library set (pandas 1.4.2, numpy 1.22.3, boto3 1.21.21) and pyarrow 7.0.0 added
through ``--additional-python-modules``; ``bond_analytics`` comes in with ``--extra-py-files``.

| source    | raw object, one per date             | lake table                              |
| --------- | ------------------------------------ | --------------------------------------- |
| auctions  | raw/auctions/date=D/auctions.json    | securities/securities.parquet, one row a CUSIP |
| fedinvest | raw/fedinvest/date=D/prices.html     | prices/date=D/prices.parquet            |
| gsw       | raw/gsw/date=D/feds200628.csv        | reference_curves/year=Y/gsw.parquet     |
| nyfed     | raw/nyfed/date=D/rates.json          | funding/year=Y/rates.parquet            |
| nt_schedule | raw/nt_schedule/date=D/schedule.pdf | sa_bonds/sa_bonds.parquet, one row a bond |
| nt_auctions | raw/nt_auctions/date=D/fixed-rate-bonds-<fiscal year>.xls(x) | sa_auctions/year=Y/sa_auctions.parquet |
| oecd      | raw/oecd/date=D/irlt.csv             | sa_reference/year=Y/sa_reference.parquet |
| imf       | raw/imf/date=D/INTGSBZAM193N.csv     | sa_reference/year=Y/sa_reference.parquet |

``control/watermarks.json`` in the lake holds, per source, the last raw date loaded. An
incremental run loads the raw dates after it, up to ``--date_filter`` when given; a full run
reloads every raw date up to ``--date_filter``. Sources load in the order above, so a day's
prices see that day's new CUSIPs. A date that fails stops its own source there, so the
watermark never skips a day; the other sources carry on, and the run fails at the end.

The loader parses, converts clean to dirty, validates and flags. It never changes a price: a
quote it doubts keeps its value and gains a flag. The same holds for the National Treasury's
figures: an auction row whose clearing yield falls outside its own bids keeps the yield, gains
``clearing_outside_bids`` and stays out of the curve. Every South African row carries its source.
The Treasury's spreadsheets and PDFs are read with openpyxl, xlrd and pypdf, which the job adds
with ``--additional-python-modules``.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import io
import json
import logging
import sys
from pathlib import Path, PurePosixPath
from typing import Dict, Iterable, List, Optional

import pandas as pd

from bond_analytics import dates, gsw, nt_auctions, nt_schedule, quality, sa_reference, schemas
from bond_analytics.fedinvest import parse_prices, parse_us_date, security_from_row
from bond_analytics.securities import static_by_cusip

log = logging.getLogger("load")

SOURCES = ("auctions", "fedinvest", "gsw", "nyfed", "nt_schedule", "nt_auctions", "oecd", "imf")
RAW_FILES = {
    "auctions": "auctions.json", "fedinvest": "prices.html", "gsw": "feds200628.csv", "nyfed": "rates.json",
    "nt_schedule": "schedule.pdf", "nt_auctions": "fixed-rate-bonds-*", "oecd": "irlt.csv", "imf": "INTGSBZAM193N.csv",
}
WATERMARKS = "control/watermarks.json"
FEDINVEST_START = dt.date(2008, 9, 1)  # its pages from 30 May to 29 August 2008 leave every type and maturity blank
FUNDING_RATES = ("SOFR", "BGCR", "TGCR")


class LoadError(RuntimeError):
    pass


# --- storage: S3, or a folder that mirrors its keys ------------------------------------------


def key(*parts: str) -> str:
    return PurePosixPath(*parts).as_posix()


class S3Store:
    def __init__(self, bucket: str, client=None):
        import boto3

        self.bucket = bucket
        self.s3 = client or boto3.client("s3")

    def list(self, prefix: str) -> List[str]:
        keys = []
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix):
            keys.extend(o["Key"] for o in page.get("Contents", []))
        return keys

    def get(self, k: str) -> Optional[bytes]:
        try:
            return self.s3.get_object(Bucket=self.bucket, Key=k)["Body"].read()
        except self.s3.exceptions.NoSuchKey:
            return None

    def put(self, k: str, body: bytes) -> None:
        self.s3.put_object(Bucket=self.bucket, Key=k, Body=body)

    def delete_prefix(self, prefix: str) -> None:
        for k in self.list(prefix):
            self.s3.delete_object(Bucket=self.bucket, Key=k)


class FolderStore:
    """A local folder whose files sit at the S3 keys, for the run without an AWS account."""

    def __init__(self, root):
        self.root = Path(root)

    def _path(self, k: str) -> Path:
        return self.root.joinpath(*PurePosixPath(k).parts)

    def list(self, prefix: str) -> List[str]:
        if not self.root.exists():
            return []
        found = (p.relative_to(self.root).as_posix() for p in self.root.rglob("*") if p.is_file())
        return sorted(k for k in found if k.startswith(prefix))

    def get(self, k: str) -> Optional[bytes]:
        p = self._path(k)
        return p.read_bytes() if p.exists() else None

    def put(self, k: str, body: bytes) -> None:
        p = self._path(k)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body)

    def delete_prefix(self, prefix: str) -> None:
        for k in self.list(prefix):
            self._path(k).unlink()


def to_parquet(df: pd.DataFrame, table: Optional[str] = None) -> bytes:
    """Parquet bytes; with ``table``, in that table's fixed schema from ``bond_analytics.schemas``."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    if table is None:
        arrow = pa.Table.from_pandas(df, preserve_index=False)
    else:
        cols = schemas.columns(table)
        frame = df[cols].astype(object).where(df[cols].notna(), None)
        arrow = pa.Table.from_pandas(frame, schema=schemas.arrow_schema(table), preserve_index=False)
    buf = io.BytesIO()
    pq.write_table(arrow, buf)
    return buf.getvalue()


def from_parquet(body: Optional[bytes]) -> Optional[pd.DataFrame]:
    return None if body is None else pd.read_parquet(io.BytesIO(body), engine="pyarrow")


# --- watermarks -------------------------------------------------------------------------------


def read_watermarks(lake) -> Dict[str, dict]:
    body = lake.get(WATERMARKS)
    return json.loads(body) if body else {}


def write_watermark(lake, source: str, loaded_through: dt.date, **extra) -> None:
    marks = read_watermarks(lake)
    marks[source] = dict(
        loaded_through=loaded_through.isoformat(),
        updated_at=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        **extra,
    )
    lake.put(WATERMARKS, json.dumps(marks, indent=2, sort_keys=True).encode())


def raw_dates(raw, source: str) -> List[dt.date]:
    out = set()
    for k in raw.list(key("raw", source) + "/"):
        part = PurePosixPath(k).parent.name
        if part.startswith("date=") and fnmatch.fnmatchcase(PurePosixPath(k).name, RAW_FILES[source]):
            out.add(dt.date.fromisoformat(part[5:]))
    return sorted(out)


def dates_to_load(raw, lake, source: str, load_type: str, date_filter: Optional[dt.date]) -> List[dt.date]:
    available = [d for d in raw_dates(raw, source) if date_filter is None or d <= date_filter]
    if load_type == "full":
        return available
    mark = read_watermarks(lake).get(source, {}).get("loaded_through")
    after = dt.date.fromisoformat(mark) if mark else dt.date.min
    return [d for d in available if d > after]


# --- securities -------------------------------------------------------------------------------

SECURITY_COLUMNS = schemas.columns("securities")


def read_static(lake) -> Dict[str, Dict[str, object]]:
    df = from_parquet(lake.get("securities/securities.parquet"))
    if df is None:
        return {}
    out = {}
    for rec in df.to_dict("records"):
        for c in ("issue_date", "dated_date", "first_coupon_date"):
            v = rec[c]
            rec[c] = None if v is None or v != v else (v if isinstance(v, dt.date) else pd.Timestamp(v).date())
        for c in ("original_term", "first_period", "auction_type"):
            rec[c] = None if rec[c] is None or rec[c] != rec[c] else rec[c]
        out[rec["cusip"]] = rec
    return out


def load_auctions(raw, lake, days: List[dt.date], rebuild: bool) -> None:
    records: List[dict] = []
    for d in days:
        body = json.loads(raw.get(key("raw", "auctions", f"date={d.isoformat()}", RAW_FILES["auctions"])))
        if "data" not in body:
            raise LoadError(f"auctions {d}: no 'data' in the response")
        records.extend(body["data"])
    current = {} if rebuild else read_static(lake)
    fresh = static_by_cusip(records)
    for cusip, rec in fresh.items():
        old = current.get(cusip)
        if old is None or (old["issue_date"] or dt.date.max) >= (rec["issue_date"] or dt.date.max):
            current[cusip] = rec
    df = pd.DataFrame(list(current.values()), columns=SECURITY_COLUMNS).sort_values("cusip")
    lake.put("securities/securities.parquet", to_parquet(df, "securities"))
    log.info("securities: %d CUSIPs after %d auction records", len(df), len(records))


# --- prices -----------------------------------------------------------------------------------

PRICE_COLUMNS = schemas.columns("prices")


def clean_prices(html: str, day: dt.date, static: Dict[str, Dict[str, object]]) -> pd.DataFrame:
    """One day's FedInvest page as validated rows. Raises ``LoadError`` on a page it cannot trust."""
    rows = parse_prices(html, day)
    if not rows:
        return pd.DataFrame(columns=PRICE_COLUMNS)
    cusips = [r["cusip"] for r in rows]
    dupes = sorted({c for c in cusips if cusips.count(c) > 1})
    if dupes:
        raise LoadError(f"fedinvest {day}: CUSIPs listed twice: {dupes[:5]}")
    settle = dates.settlement_date(day)
    out = []
    for r in rows:
        sec = security_from_row(r, static.get(r["cusip"]))
        clean = float(r["end_of_day"])
        flags = []
        if sec.cusip not in static:
            flags.append("no_auction_record")
        if sec.maturity <= settle:
            flags.append("matured")
        if clean <= 0:
            flags.append("no_price")
        priced = clean > 0 and sec.maturity > settle
        accrued = dates.accrued_interest(sec, settle) if priced else None
        out.append({
            "security": sec, "price_date": day, "settle_date": settle, "cusip": sec.cusip, "kind": sec.kind,
            "security_type": r["security_type"], "coupon": sec.coupon, "maturity": sec.maturity,
            "call_date": parse_us_date(r["call_date"]), "callable": parse_us_date(r["call_date"]) is not None,
            "buy": float(r["buy"]), "sell": float(r["sell"]), "end_of_day": clean, "clean": clean,
            "accrued": accrued, "dirty": clean + accrued if priced else None,
            "years": dates.year_fraction(settle, sec.maturity), "original_term": sec.original_term,
            "issue_date": sec.issue_date, "first_period": sec.first_period, "flags": flags,
        })
    priced_rows = [r for r in out if r["dirty"] is not None]
    quality.flag_quotes(priced_rows, settle)
    for r in out:
        r.setdefault("bill_curve_gap", None)
        r.setdefault("neighbour_gap_ticks", None)
        r["quality_flags"] = ",".join(r.pop("flags"))
    return pd.DataFrame(out)[PRICE_COLUMNS]


def load_fedinvest(raw, lake, days: List[dt.date], static) -> Optional[dt.date]:
    """Load each date in turn; return the last one loaded. Stops at the first that fails."""
    last = None
    for d in days:
        html = raw.get(key("raw", "fedinvest", f"date={d.isoformat()}", RAW_FILES["fedinvest"])).decode("latin-1")
        df = clean_prices(html, d, static)
        part = key("prices", f"date={d.isoformat()}", "prices.parquet")
        if df.empty:
            lake.delete_prefix(key("prices", f"date={d.isoformat()}") + "/")
            log.info("fedinvest %s: no prices published", d)
        else:
            lake.put(part, to_parquet(df, "prices"))
            flagged = int((df["quality_flags"] != "").sum())
            log.info("fedinvest %s: %d securities, %d flagged", d, len(df), flagged)
        write_watermark(lake, "fedinvest", d)
        last = d
    return last


# --- reference curves and funding: yearly files, rows upserted by date ------------------------


def _as_date(v):
    return v if isinstance(v, dt.date) or v is None else pd.Timestamp(v).date()


def upsert_by_year(lake, table: str, name: str, df: pd.DataFrame, key_cols: List[str], date_col: str = "date") -> None:
    df = df.reindex(columns=schemas.columns(table))
    for year, part in df.groupby(df[date_col].map(lambda d: d.year)):
        k = key(table, f"year={year}", name)
        old = from_parquet(lake.get(k))
        if old is not None:
            old[date_col] = old[date_col].map(_as_date)
            part = pd.concat([old, part], ignore_index=True)
        part = part.drop_duplicates(subset=key_cols, keep="last").sort_values(key_cols)
        lake.put(k, to_parquet(part.reset_index(drop=True), table))


def load_gsw(raw, lake, days: List[dt.date], rebuild: bool) -> None:
    """Upsert the curve rows the newest raw file adds; a rebuild writes every year again."""
    newest = days[-1]
    text = raw.get(key("raw", "gsw", f"date={newest.isoformat()}", RAW_FILES["gsw"])).decode("utf-8")
    mark = read_watermarks(lake).get("gsw", {}).get("curve_date")
    since = dt.date.min if rebuild or not mark else dt.date.fromisoformat(mark)
    rows = [r for r in gsw.read_rows(text) if r["date"] > since and r["beta0"] is not None]
    if rebuild:
        lake.delete_prefix("reference_curves/")
    if rows:
        upsert_by_year(lake, "reference_curves", "gsw.parquet", pd.DataFrame(rows), ["date"])
    curve_date = max([r["date"] for r in rows], default=dt.date.fromisoformat(mark) if mark else None)
    log.info("gsw: %d curve dates added from the file of %s", len(rows), newest)
    write_watermark(lake, "gsw", newest, **({"curve_date": curve_date.isoformat()} if curve_date else {}))


def load_nyfed(raw, lake, days: List[dt.date], rebuild: bool) -> None:
    rows = []
    for d in days:
        body = json.loads(raw.get(key("raw", "nyfed", f"date={d.isoformat()}", RAW_FILES["nyfed"])))
        for r in body["refRates"]:
            if r.get("type") not in FUNDING_RATES:
                continue  # SOFRAI, the averages and index, has no daily rate
            rows.append({"date": dt.date.fromisoformat(r["effectiveDate"]), "rate_type": r["type"],
                         "percent_rate": float(r["percentRate"]),
                         "volume_bn": float(r["volumeInBillions"]) if r.get("volumeInBillions") not in (None, "") else None})
    if rebuild:
        lake.delete_prefix("funding/")
    if rows:
        upsert_by_year(lake, "funding", "rates.parquet", pd.DataFrame(rows), ["date", "rate_type"])
    write_watermark(lake, "nyfed", days[-1])


# --- South Africa: the Treasury's schedule and auctions, the OECD and IMF series --------------


def _raw_objects(raw, source: str, day: dt.date) -> List[str]:
    prefix = key("raw", source, f"date={day.isoformat()}") + "/"
    return [k for k in raw.list(prefix) if fnmatch.fnmatchcase(PurePosixPath(k).name, RAW_FILES[source])]


def read_sa_bonds(lake) -> Dict[str, dict]:
    df = from_parquet(lake.get("sa_bonds/sa_bonds.parquet"))
    if df is None:
        return {}
    out = {}
    for rec in df.to_dict("records"):
        for c in ("issue_date", "pricing_maturity", "schedule_date"):
            rec[c] = _as_date(rec[c]) if rec[c] == rec[c] else None
        rec["redemptions"] = [dt.date.fromisoformat(x) for x in rec["redemptions"].split(",")]
        out[rec["bond_code"]] = rec
    return out


def write_sa_bonds(lake, bonds: Dict[str, dict]) -> None:
    rows = [{**b, "redemptions": ",".join(d.isoformat() for d in b["redemptions"])} for b in bonds.values()]
    df = pd.DataFrame(rows, columns=schemas.columns("sa_bonds")).sort_values("bond_code")
    lake.put("sa_bonds/sa_bonds.parquet", to_parquet(df.reset_index(drop=True), "sa_bonds"))


def load_nt_schedule(raw, lake, days: List[dt.date], rebuild: bool) -> None:
    """Every schedule in the run, merged over what the lake holds: each bond from its latest schedule."""
    parsed = [] if rebuild else [read_sa_bonds(lake)]
    for d in days:
        for k in _raw_objects(raw, "nt_schedule", d):
            one = nt_schedule.parse_pdf(raw.get(k))
            if not one:
                raise LoadError(f"nt_schedule {d}: no fixed-rate bond lines in {k}")
            parsed.append(one)
    merged = nt_schedule.merge(parsed)
    write_sa_bonds(lake, merged)
    flagged = sorted(c for c, b in merged.items() if b["quality_flags"])
    log.info("nt_schedule: %d fixed-rate bonds, flagged %s", len(merged), flagged or "none")


def read_sa_auctions(lake) -> List[dict]:
    rows = []
    for k in lake.list("sa_auctions/"):
        if not k.endswith("sa_auctions.parquet"):
            continue
        df = from_parquet(lake.get(k))
        df = df.astype(object).where(df.notna(), None)
        for rec in df.to_dict("records"):
            rec["auction_date"], rec["settle_date"] = _as_date(rec["auction_date"]), _as_date(rec["settle_date"])
            rows.append(rec)
    return rows


def write_sa_auctions(lake, rows: List[dict], rebuild: bool = False) -> List[dict]:
    """Merge new auction rows with the lake's, check every bond against its neighbouring auctions
    afresh, and write one file a year. A later row for the same auction date and bond replaces an
    earlier one, unless it would lose the best and worst bids the earlier one has."""
    merged = nt_auctions.deduplicate(([] if rebuild else read_sa_auctions(lake)) + list(rows))
    merged = nt_auctions.flag_neighbours(merged)
    lake.delete_prefix("sa_auctions/")
    cols = schemas.columns("sa_auctions")
    for year in sorted({r["auction_date"].year for r in merged}):
        df = pd.DataFrame([r for r in merged if r["auction_date"].year == year], columns=cols)
        lake.put(key("sa_auctions", f"year={year}", "sa_auctions.parquet"), to_parquet(df, "sa_auctions"))
    return merged


def load_nt_auctions(raw, lake, days: List[dt.date], rebuild: bool) -> None:
    bonds = read_sa_bonds(lake)
    if not bonds:
        raise LoadError("nt_auctions: no sa_bonds in the lake to check the auctions against; load nt_schedule first")
    rows: List[dict] = []
    for d in days:
        for k in sorted(_raw_objects(raw, "nt_auctions", d)):
            rows.extend(nt_auctions.parse_file(raw.get(k), PurePosixPath(k).name))
    rows = nt_auctions.deduplicate(nt_auctions.flag(rows, bonds))
    merged = write_sa_auctions(lake, rows, rebuild)
    bad = sum(1 for r in merged if not nt_auctions.usable(r))
    log.info("nt_auctions: %d bond auctions in the lake, %d kept out of the curve by their flags", len(merged), bad)


def load_reference(raw, lake, source: str, days: List[dt.date], rebuild: bool) -> None:
    """The newest file of a monthly series replaces that series' months in ``sa_reference``."""
    newest = days[-1]
    body = raw.get(key("raw", source, f"date={newest.isoformat()}", RAW_FILES[source]))
    rows = sa_reference.parse_oecd(body, newest) if source == "oecd" else sa_reference.parse_imf(body)
    if rebuild:
        series = rows[0]["series"]
        for k in lake.list("sa_reference/"):
            old = from_parquet(lake.get(k))
            keep = old[old["series"] != series]
            lake.put(k, to_parquet(keep.reset_index(drop=True), "sa_reference"))
    upsert_by_year(lake, "sa_reference", "sa_reference.parquet", pd.DataFrame(rows), ["date", "series"])
    log.info("%s: %d months, %s to %s", source, len(rows), rows[0]["date"], rows[-1]["date"])
    write_watermark(lake, source, newest)


# --- the job ----------------------------------------------------------------------------------


def run(raw, lake, load_type: str, date_filter: Optional[dt.date] = None, sources: Iterable[str] = SOURCES) -> Dict[str, str]:
    if load_type not in ("full", "incremental"):
        raise LoadError(f"load_type must be full or incremental, not {load_type!r}")
    sources = [s for s in SOURCES if s in set(sources)]
    status: Dict[str, str] = {}
    for source in sources:
        days = dates_to_load(raw, lake, source, load_type, date_filter)
        if source == "fedinvest":
            days = [d for d in days if d >= FEDINVEST_START]
        if not days:
            status[source] = "nothing new"
            continue
        try:
            if source == "auctions":
                load_auctions(raw, lake, days, rebuild=load_type == "full")
                write_watermark(lake, "auctions", days[-1])
            elif source == "fedinvest":
                if load_type == "full":
                    lake.delete_prefix("prices/")
                load_fedinvest(raw, lake, days, read_static(lake))
            elif source == "gsw":
                load_gsw(raw, lake, days, rebuild=load_type == "full")
            elif source == "nyfed":
                load_nyfed(raw, lake, days, rebuild=load_type == "full")
            elif source == "nt_schedule":
                load_nt_schedule(raw, lake, days, rebuild=load_type == "full")
                write_watermark(lake, "nt_schedule", days[-1])
            elif source == "nt_auctions":
                load_nt_auctions(raw, lake, days, rebuild=load_type == "full")
                write_watermark(lake, "nt_auctions", days[-1])
            elif source in ("oecd", "imf"):
                load_reference(raw, lake, source, days, rebuild=load_type == "full")
            status[source] = f"loaded {days[0]} to {days[-1]}"
        except Exception as exc:
            log.exception("%s failed", source)
            status[source] = f"failed: {type(exc).__name__}: {exc}"
    log.info(json.dumps(status))
    failed = {s: v for s, v in status.items() if v.startswith("failed")}
    if failed:
        raise LoadError(f"sources failed: {failed}")
    return status


def resolve_args(argv: List[str]) -> dict:
    """``getResolvedOptions`` on Glue, as the lab's job; argparse with the same names elsewhere."""
    required = ["raw_bucket", "lake_bucket", "load_type"]
    optional = ["date_filter", "sources", "local_root"]
    try:
        from awsglue.utils import getResolvedOptions
    except ImportError:
        ap = argparse.ArgumentParser(description="Load the raw zone into the lake")
        for name in required:
            ap.add_argument(f"--{name}", required=name == "load_type", default=None)
        for name in optional:
            ap.add_argument(f"--{name}", default=None)
        return vars(ap.parse_known_args(argv[1:])[0])
    args = getResolvedOptions(argv, required)
    for name in optional:
        if f"--{name}" in argv:
            args.update(getResolvedOptions(argv, [name]))
        else:
            args[name] = None
    return args


def main(argv=None) -> Dict[str, str]:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    args = resolve_args(list(sys.argv if argv is None else argv))
    if args.get("local_root"):
        root = Path(args["local_root"])
        raw, lake = FolderStore(root / "raw-bucket"), FolderStore(root / "lake-bucket")
    else:
        raw, lake = S3Store(args["raw_bucket"]), S3Store(args["lake_bucket"])
    date_filter = dt.date.fromisoformat(args["date_filter"]) if args.get("date_filter") else None
    sources = args["sources"].split(",") if args.get("sources") else SOURCES
    return run(raw, lake, args["load_type"], date_filter, sources)


if __name__ == "__main__":
    main()
