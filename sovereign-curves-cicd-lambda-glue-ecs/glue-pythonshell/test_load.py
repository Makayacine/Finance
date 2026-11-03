"""The loader on moto's S3, with ``awsglue`` stood in through ``sys.modules`` as Glue provides it.

The FedInvest pages here are built from a flat 4% curve, rounded to 32nds as FedInvest quotes,
so every dirty price and every flag has an answer known before the loader runs.
"""

import datetime as dt
import io
import json
import math
import sys
import types
from pathlib import Path

import boto3
import pandas as pd
import pytest
from moto import mock_aws

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import load  # noqa: E402
from bond_analytics import dates  # noqa: E402
from bond_analytics.dates import Security  # noqa: E402

D1, D2, D3 = dt.date(2026, 10, 22), dt.date(2026, 10, 23), dt.date(2026, 10, 26)
HEADER = "<tr><th>CUSIP</th><th>SECURITY TYPE</th><th>RATE</th><th>MATURITY DATE</th><th>CALL DATE</th><th>BUY</th><th>SELL</th><th>END OF DAY</th></tr>"
TYPES = {"bill": "MARKET BASED BILL", "note": "MARKET BASED NOTE", "bond": "MARKET BASED BOND"}

BILLS = ([("W%02d" % k, dt.date(2026, 10, 29) + dt.timedelta(days=7 * k)) for k in range(1, 9)]
         + [("M%02d" % m, dt.date(2026, 12, 24) + dt.timedelta(days=28 * m)) for m in range(1, 13)])
NOTES = [("N1", 2.0, dt.date(2026, 11, 15)), ("N2", 4.625, dt.date(2026, 11, 15)), ("N3", 3.0, dt.date(2026, 11, 30)),
         ("N4", 1.5, dt.date(2026, 12, 31)), ("N5", 4.0, dt.date(2027, 4, 15)), ("N6", 3.5, dt.date(2027, 5, 15)),
         ("N7", 4.25, dt.date(2028, 10, 31))]
ODD = ("OB", 6.5, dt.date(2026, 11, 15))  # the old bond FedInvest quotes seven 32nds low


def fair_clean(sec, day, rate=0.04):
    settle = dates.settlement_date(day)
    value = sum(a * math.exp(-rate * dates.year_fraction(settle, d)) for d, a in dates.cash_flows(sec, settle))
    return value - dates.accrued_interest(sec, settle)


def page(day, odd_ticks=-7, extra_rows=(), show_date=None):
    rows = []
    for cusip, mat in BILLS:
        sec = Security(cusip, "bill", 0.0, mat)
        rows.append((cusip, "bill", 0.0, mat, f"{fair_clean(sec, day):.6f}"))
    for cusip, cpn, mat in NOTES:
        sec = Security(cusip, "note", cpn, mat)
        rows.append((cusip, "note", cpn, mat, f"{round(fair_clean(sec, day) * 32) / 32:.6f}"))
    cusip, cpn, mat = ODD
    sec = Security(cusip, "bond", cpn, mat)
    rows.append((cusip, "bond", cpn, mat, f"{(round(fair_clean(sec, day) * 32) + odd_ticks) / 32:.6f}"))
    rows.extend(extra_rows)
    body = "".join(
        f"<tr><td>{c}</td><td>{TYPES[k]}</td><td>{cp:.3f}%</td><td>{m:%m/%d/%Y}</td><td></td><td>0.000000</td><td>{p}</td><td>{p}</td></tr>"
        for c, k, cp, m, p in rows
    )
    shown = (show_date or day).strftime("%B %d, %Y").replace(" 0", " ")
    return f'<h2>Prices For: {shown}</h2><table class="data1">{HEADER}{body}</table>'.encode("latin-1")


HOLIDAY = b"<h2>Prices For: October 12, 2026</h2><p>no table</p>"
AUCTIONS = {"data": [
    {"cusip": "N7", "security_type": "Note", "original_security_term": "2-Year", "issue_date": "2026-10-31",
     "dated_date": "2026-10-31", "first_int_payment_date": "2027-04-30", "first_int_period": "Normal", "reopening": "No",
     "inflation_index_security": "No", "floating_rate": "No"},
]}
GSW_TEXT = (
    '"Note"\n\nDate,BETA0,BETA1,BETA2,BETA3,SVENY01,TAU1,TAU2\n'
    "2025-12-31,4,0.5,1,1,4.2,1.5,10\n2026-10-21,4.1,0.4,1,1,4.3,1.5,10\n2026-10-22,4.2,0.4,1,1,4.4,1.5,10\n"
)


@pytest.fixture
def buckets():
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        for b in ("curves-raw", "curves-lake"):
            s3.create_bucket(Bucket=b)
        yield load.S3Store("curves-raw", s3), load.S3Store("curves-lake", s3)


def put_raw(raw, source, day, body):
    raw.put(load.key("raw", source, f"date={day.isoformat()}", load.RAW_FILES[source]), body)


def prices(lake, day):
    return load.from_parquet(lake.get(f"prices/date={day.isoformat()}/prices.parquet"))


def marks(lake):
    return {s: m["loaded_through"] for s, m in load.read_watermarks(lake).items()}


def test_glue_arguments_come_from_get_resolved_options(monkeypatch):
    seen = []

    def get_resolved_options(argv, names):
        seen.append(list(names))
        return {n: argv[argv.index(f"--{n}") + 1] for n in names}

    awsglue = types.ModuleType("awsglue")
    utils = types.ModuleType("awsglue.utils")
    utils.getResolvedOptions = get_resolved_options
    monkeypatch.setitem(sys.modules, "awsglue", awsglue)
    monkeypatch.setitem(sys.modules, "awsglue.utils", utils)
    argv = ["load.py", "--raw_bucket", "r", "--lake_bucket", "l", "--load_type", "incremental", "--date_filter", "2026-10-23"]
    args = load.resolve_args(argv)
    assert args == {"raw_bucket": "r", "lake_bucket": "l", "load_type": "incremental", "date_filter": "2026-10-23",
                    "sources": None, "local_root": None}
    assert seen == [["raw_bucket", "lake_bucket", "load_type"], ["date_filter"]]


def test_incremental_load_writes_dirty_prices_and_the_watermark(buckets):
    raw, lake = buckets
    put_raw(raw, "auctions", D1, json.dumps(AUCTIONS).encode())
    put_raw(raw, "fedinvest", D1, page(D1))
    put_raw(raw, "fedinvest", D2, page(D2))
    status = load.run(raw, lake, "incremental", sources=["auctions", "fedinvest"])
    assert status == {"auctions": "loaded 2026-10-22 to 2026-10-22", "fedinvest": "loaded 2026-10-22 to 2026-10-23"}
    assert sorted(k for k in lake.list("prices/")) == ["prices/date=2026-10-22/prices.parquet", "prices/date=2026-10-23/prices.parquet"]
    df = prices(lake, D2).set_index("cusip")
    n5 = Security("N5", "note", 4.0, dt.date(2027, 4, 15))
    settle = dt.date(2026, 10, 26)
    assert df.loc["N5", "settle_date"] == settle
    assert df.loc["N5", "accrued"] == pytest.approx(2.0 * (settle - dt.date(2026, 10, 15)).days / 182)
    assert df.loc["N5", "dirty"] == pytest.approx(df.loc["N5", "clean"] + df.loc["N5", "accrued"])
    assert dates.accrued_interest(n5, settle) == pytest.approx(df.loc["N5", "accrued"])
    assert df.loc["N7", "original_term"] == "2-Year" and "no_auction_record" not in df.loc["N7", "quality_flags"]
    assert df.loc["N5", "quality_flags"] == "no_auction_record"
    assert marks(lake) == {"auctions": "2026-10-22", "fedinvest": "2026-10-23"}
    assert load.read_watermarks(lake)["fedinvest"]["updated_at"].endswith("Z")


def test_a_second_incremental_run_has_nothing_new(buckets):
    raw, lake = buckets
    put_raw(raw, "fedinvest", D1, page(D1))
    load.run(raw, lake, "incremental", sources=["fedinvest"])
    before = lake.get("prices/date=2026-10-22/prices.parquet")
    assert load.run(raw, lake, "incremental", sources=["fedinvest"]) == {"fedinvest": "nothing new"}
    assert lake.get("prices/date=2026-10-22/prices.parquet") == before


def test_date_filter_stops_the_load_and_full_reloads(buckets):
    raw, lake = buckets
    for d in (D1, D2, D3):
        put_raw(raw, "fedinvest", d, page(d))
    load.run(raw, lake, "incremental", date_filter=D2, sources=["fedinvest"])
    assert marks(lake)["fedinvest"] == "2026-10-23"
    assert prices(lake, D3) is None
    lake.put("prices/date=2026-10-22/prices.parquet", b"stale")
    load.run(raw, lake, "full", sources=["fedinvest"])
    assert len(prices(lake, D1)) == len(prices(lake, D3)) == len(BILLS) + len(NOTES) + 1
    assert marks(lake)["fedinvest"] == "2026-10-26"


def test_a_holiday_writes_nothing_and_moves_the_watermark(buckets):
    raw, lake = buckets
    put_raw(raw, "fedinvest", dt.date(2026, 10, 12), HOLIDAY)
    assert load.run(raw, lake, "incremental", sources=["fedinvest"]) == {"fedinvest": "loaded 2026-10-12 to 2026-10-12"}
    assert lake.list("prices/") == []
    assert marks(lake)["fedinvest"] == "2026-10-12"


def test_a_bad_page_stops_its_source_there_and_fails_the_run(buckets):
    raw, lake = buckets
    put_raw(raw, "fedinvest", D1, page(D1))
    put_raw(raw, "fedinvest", D2, page(D2, show_date=D1))  # the server answered with the wrong day
    put_raw(raw, "fedinvest", D3, page(D3))
    put_raw(raw, "gsw", D3, GSW_TEXT.encode())
    with pytest.raises(load.LoadError, match="fedinvest"):
        load.run(raw, lake, "incremental", sources=["fedinvest", "gsw"])
    assert marks(lake) == {"fedinvest": "2026-10-22", "gsw": "2026-10-26"}
    assert prices(lake, D3) is None  # never past the gap
    assert lake.list("reference_curves/") == ["reference_curves/year=2025/gsw.parquet", "reference_curves/year=2026/gsw.parquet"]


def test_a_cusip_listed_twice_is_refused(buckets):
    raw, lake = buckets
    dup = (("N1", "note", 2.0, dt.date(2026, 11, 15), "99.875000"),)
    put_raw(raw, "fedinvest", D1, page(D1, extra_rows=dup))
    with pytest.raises(load.LoadError, match="listed twice"):
        load.run(raw, lake, "incremental", sources=["fedinvest"])
    assert "fedinvest" not in load.read_watermarks(lake)


def test_the_odd_quote_is_flagged_and_kept(buckets):
    raw, lake = buckets
    put_raw(raw, "fedinvest", D2, page(D2, odd_ticks=-7))
    load.run(raw, lake, "incremental", sources=["fedinvest"])
    df = prices(lake, D2).set_index("cusip")
    sec = Security("OB", "bond", 6.5, dt.date(2026, 11, 15))
    assert df.loc["OB", "clean"] == (round(fair_clean(sec, D2) * 32) - 7) / 32  # the quote, unchanged
    assert "off_neighbours" in df.loc["OB", "quality_flags"]
    assert df.loc["OB", "neighbour_gap_ticks"] == pytest.approx(-7, abs=1.0)
    notes = df[df.kind == "note"]
    assert not notes["quality_flags"].str.contains("off_neighbours").any()
    assert notes["neighbour_gap_ticks"].dropna().abs().max() < 1.0


def test_a_quote_three_ticks_off_is_rounding_not_a_flag(buckets):
    raw, lake = buckets
    put_raw(raw, "fedinvest", D2, page(D2, odd_ticks=-3))
    load.run(raw, lake, "incremental", sources=["fedinvest"])
    assert "off_neighbours" not in prices(lake, D2).set_index("cusip").loc["OB", "quality_flags"]


def test_gsw_adds_only_new_curve_dates(buckets):
    raw, lake = buckets
    put_raw(raw, "gsw", D1, GSW_TEXT.encode())
    load.run(raw, lake, "incremental", sources=["gsw"])
    assert load.read_watermarks(lake)["gsw"]["curve_date"] == "2026-10-22"
    newer = GSW_TEXT.replace("2026-10-22,4.2", "2026-10-22,9.9") + "2026-10-23,4.3,0.4,1,1,4.5,1.5,10\n"
    put_raw(raw, "gsw", D2, newer.encode())
    load.run(raw, lake, "incremental", sources=["gsw"])
    y2026 = load.from_parquet(lake.get("reference_curves/year=2026/gsw.parquet"))
    assert list(y2026["date"].astype(str)) == ["2026-10-21", "2026-10-22", "2026-10-23"]
    assert list(y2026["beta0"]) == [4.1, 4.2, 4.3]  # an old row is not rewritten by an incremental load
    load.run(raw, lake, "full", sources=["gsw"])
    y2026 = load.from_parquet(lake.get("reference_curves/year=2026/gsw.parquet"))
    assert list(y2026["beta0"]) == [4.1, 9.9, 4.3]  # a full load takes the newest file's history


def test_nyfed_rates_upsert_by_date_and_type(buckets):
    raw, lake = buckets
    body = {"refRates": [{"effectiveDate": "2026-10-22", "type": "SOFR", "percentRate": 3.95, "volumeInBillions": 3100},
                         {"effectiveDate": "2026-10-22", "type": "TGCR", "percentRate": 3.9, "volumeInBillions": ""},
                         {"effectiveDate": "2026-10-22", "type": "SOFRAI", "average30day": 3.94, "index": 1.25}]}
    put_raw(raw, "nyfed", D1, json.dumps(body).encode())
    put_raw(raw, "nyfed", D2, json.dumps(body).encode())
    load.run(raw, lake, "incremental", sources=["nyfed"])
    df = load.from_parquet(lake.get("funding/year=2026/rates.parquet"))
    assert len(df) == 2 and df["volume_bn"].isna().sum() == 1


def test_folder_store_keys_are_posix(tmp_path):
    store = load.FolderStore(tmp_path)
    store.put("prices/date=2026-10-23/prices.parquet", b"x")
    assert store.list("prices/") == ["prices/date=2026-10-23/prices.parquet"]
    assert (tmp_path / "prices" / "date=2026-10-23" / "prices.parquet").exists()
    store.delete_prefix("prices/")
    assert store.list("") == []


def test_unknown_load_type_is_refused(buckets):
    raw, lake = buckets
    with pytest.raises(load.LoadError, match="load_type"):
        load.run(raw, lake, "delta")
