"""The engine on moto's S3, over two real days from the committed sample.

The sample's 23 and 26 October 2026 are staged into a moto raw bucket, loaded with the Glue job's
own ``run`` and then run through the engine: GSW published 23 October and not the 26th, so the
two days cover a curve with an answer key and one without.
"""

import datetime as dt
import gzip
import sys
import warnings
from pathlib import Path

import boto3
import pandas as pd
import pytest
from moto import mock_aws

warnings.simplefilter("ignore", SyntaxWarning)
HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
for p in (PROJECT, PROJECT / "glue-pythonshell", PROJECT / "lambda-functions", HERE):
    sys.path.insert(0, str(p))

import app  # noqa: E402
import load  # noqa: E402
from bond_analytics import dates, svensson  # noqa: E402
from bond_analytics.dates import Security  # noqa: E402
from bond_analytics.market import Quote  # noqa: E402

SAMPLE = PROJECT / "data" / "sample"
DAYS = ("2026-10-23", "2026-10-26")


def stage(raw, days=DAYS):
    for path in SAMPLE.rglob("*.gz"):
        k = path.relative_to(SAMPLE).with_suffix("").as_posix()
        if "/fedinvest/" in "/" + k and not any(d in k for d in days):
            continue
        if "/nyfed/" in "/" + k and not any(d in k for d in days):
            continue
        raw.put(k, gzip.decompress(path.read_bytes()))


@pytest.fixture(scope="module")
def lake():
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        for b in ("curves-raw", "curves-lake"):
            s3.create_bucket(Bucket=b)
        raw, lake = load.S3Store("curves-raw", s3), load.S3Store("curves-lake", s3)
        stage(raw)
        load.run(raw, lake, "full")
        summary = app.run(lake, "incremental")
        yield lake, summary


def table(lake, name, day):
    return load.from_parquet(lake.get(f"{name}/date={day}/{name}.parquet"))


def test_every_table_lands_at_its_key_for_both_days(lake):
    lake, summary = lake
    assert summary["dates"] == 2
    for name in app.TABLES:
        assert sorted(k for k in lake.list(name + "/")) == [f"{name}/date={d}/{name}.parquet" for d in DAYS]
    assert load.read_watermarks(lake)["engine"]["loaded_through"] == "2026-10-26"


def test_a_second_run_finds_nothing_new(lake):
    lake, _ = lake
    assert app.run(lake, "incremental")["dates"] == 0


def test_our_fit_lands_on_gsw(lake):
    lake, _ = lake
    curves = table(lake, "curves", "2026-10-23").pivot(index="tenor", columns="curve", values="zero_rate")
    gap = (curves["svensson"] - curves["gsw"]).abs() * 100
    assert gap.loc[[2.0, 5.0, 10.0, 30.0]].max() < 3.0  # basis points


def test_gsw_rows_only_where_gsw_published(lake):
    lake, _ = lake
    assert set(table(lake, "fit_errors", "2026-10-23")["curve"]) == {"svensson", "gsw", "fia_bootstrap"}
    assert set(table(lake, "fit_errors", "2026-10-26")["curve"]) == {"svensson", "fia_bootstrap"}


def test_a_fit_on_a_bound_is_recorded(lake):
    lake, summary = lake
    params = table(lake, "curve_params", "2026-10-23").set_index("curve")
    assert params.loc["svensson", "params_at_bound"] == "beta0"  # the long rate's floor at zero, as GSW's own fit
    errors = table(lake, "fit_errors", "2026-10-23")
    assert set(errors.loc[errors.curve == "svensson", "params_at_bound"]) == {"beta0"}
    assert "2026-10-23:beta0" in summary["fits_on_a_bound"]


def test_the_bootstrap_reprices_its_own_bonds(lake):
    lake, _ = lake
    errors = table(lake, "fit_errors", "2026-10-26")
    knots = errors[(errors.curve == "fia_bootstrap") & errors.in_fit]
    assert 40 <= len(knots) <= 50
    assert knots["error_price"].abs().max() < 1e-4


def test_key_rates_add_up_and_funding_is_sofr(lake):
    lake, _ = lake
    rk = table(lake, "risk", "2026-10-26")
    krd = rk[[c for c in rk.columns if c.startswith("krd_")]].sum(axis=1)
    assert ((krd - rk["curve_dv01"]).abs() / rk["curve_dv01"]).max() < 1e-5
    assert set(rk["kind"]) == {"bill", "note", "bond"}
    params = table(lake, "curve_params", "2026-10-26").set_index("curve")
    assert params.loc["svensson", "funding_source"] == "SOFR"


def quote(kind, coupon, maturity, dirty, day=dt.date(2015, 6, 1)):
    sec = Security("X", kind, coupon, maturity)
    settle = dates.settlement_date(day)
    return Quote(sec, day, settle, dirty - dates.accrued_interest(sec, settle), dates.accrued_interest(sec, settle), dirty,
                 False, None, dates.year_fraction(settle, maturity), dates.to_relative_bond(sec, settle, dirty))


def test_before_sofr_the_four_week_bill_funds():
    day = dt.date(2015, 6, 1)
    bills = [quote("bill", 0.0, dt.date(2015, 6, 30), 99.99), quote("bill", 0.0, dt.date(2015, 9, 3), 99.95)]
    rate, source = app.funding_rate(day, bills, {dt.date(2015, 6, 1): 9.99})
    assert source == "4-week bill"
    assert rate == pytest.approx((100 / 99.99 - 1) * 360 / 28 * 100)
    assert app.funding_rate(dt.date(2026, 10, 26), bills, {dt.date(2026, 10, 23): 3.9}) == (3.9, "SOFR")


def test_carry_is_coupon_income_less_financing():
    q = quote("note", 4.0, dt.date(2020, 11, 15), 102.0)  # settles 2 June 2015, no coupon in the next 30 days
    expected = 2.0 * 30 / 184 - 102.0 * 0.03 * 30 / 360
    assert app.carry(q, 3.0) == pytest.approx(expected)
    assert app.carry(quote("bill", 0.0, dt.date(2015, 9, 3), 99.9), 3.0) is None


class FakeFetcher:
    """The fetch module's backfill surface, answering from the committed sample."""

    def __init__(self):
        self.calls = []

    def backfill(self, days, put, pause_seconds, sources):
        out = {}
        for d in days:
            path = SAMPLE / "raw" / "fedinvest" / f"date={d.isoformat()}" / "prices.html.gz"
            if path.exists():
                put(f"raw/fedinvest/date={d.isoformat()}/prices.html", gzip.decompress(path.read_bytes()))
                out[d] = {"fedinvest": "ok"}
            else:
                out[d] = {"fedinvest": "failed: no sample"}
        self.calls.append((days[0], days[-1], pause_seconds))
        return out

    def fetch_auction_history(self):
        return gzip.decompress((SAMPLE / "raw/auctions/date=2026-11-02/auctions.json.gz").read_bytes())

    def fetch_gsw(self, day):
        return gzip.decompress((SAMPLE / "raw/gsw/date=2026-11-02/feds200628.csv.gz").read_bytes())


def test_backfill_fetches_loads_and_runs_the_range(tmp_path):
    raw, lake = load.FolderStore(tmp_path / "raw"), load.FolderStore(tmp_path / "lake")
    fake = FakeFetcher()
    summary = app.backfill(raw, lake, dt.date(2026, 10, 9), dt.date(2026, 10, 14), pause_seconds=2.0, fetcher=fake)
    assert fake.calls == [(dt.date(2026, 10, 9), dt.date(2026, 10, 14), 2.0)]  # Columbus Day, the 12th, not asked
    assert summary["fetched"] == "3 of 3 business days"
    assert summary["dates"] == 3
    assert [k.split("/")[1] for k in lake.list("risk/")] == ["date=2026-10-09", "date=2026-10-13", "date=2026-10-14"]
