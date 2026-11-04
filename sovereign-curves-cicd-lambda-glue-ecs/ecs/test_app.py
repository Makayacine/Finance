"""The engine on moto's S3, over two real days from the committed sample.

The sample's 23 and 26 October 2026 are staged into a moto raw bucket, loaded with the Glue job's
own ``run`` and then run through the engine: GSW published 23 October and not the 26th, so the
two days cover a curve with an answer key and one without.
"""

import datetime as dt
import gzip
import json
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


def test_a_date_with_no_price_gets_no_curve_and_the_run_goes_on(tmp_path):
    """FedInvest's end-of-day column is zero on every row of 12 September and 21 November 2014."""
    raw, lake = load.FolderStore(tmp_path / "raw"), load.FolderStore(tmp_path / "lake")
    stage(raw)
    load.run(raw, lake, "full")
    k = "prices/date=2026-10-23/prices.parquet"
    prices = load.from_parquet(lake.get(k))
    prices["dirty"] = float("nan")  # what the loader writes for a quote with no price
    lake.put(k, load.to_parquet(prices, "prices"))
    summary = app.run(lake, "full")
    assert summary["unpriced"] == ["2026-10-23"]
    assert [k.split("/")[1] for k in lake.list("risk/")] == ["date=2026-10-26"]
    assert load.read_watermarks(lake)["engine"]["loaded_through"] == "2026-10-26"


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

    def fetch_nyfed(self, day, start=None):
        """The range in one body, as the API answers it: the sample's days from ``start`` to ``day``."""
        self.nyfed = (start, day)
        rates = []
        for path in sorted((SAMPLE / "raw" / "nyfed").glob("date=*/rates.json.gz")):
            if start <= dt.date.fromisoformat(path.parent.name[5:]) <= day:
                rates.extend(json.loads(gzip.decompress(path.read_bytes()))["refRates"])
        return json.dumps({"refRates": rates}).encode()


def test_backfill_fetches_loads_and_runs_the_range(tmp_path):
    raw, lake = load.FolderStore(tmp_path / "raw"), load.FolderStore(tmp_path / "lake")
    fake = FakeFetcher()
    summary = app.backfill(raw, lake, dt.date(2026, 10, 9), dt.date(2026, 10, 14), pause_seconds=2.0, fetcher=fake)
    assert fake.calls == [(dt.date(2026, 10, 9), dt.date(2026, 10, 14), 2.0)]  # Columbus Day, the 12th, not asked
    assert fake.nyfed == (dt.date(2026, 10, 9), dt.date(2026, 10, 14))  # the range in one request
    assert summary["fetched"] == "3 of 3 business days"
    assert summary["dates"] == 3
    assert [k.split("/")[1] for k in lake.list("risk/")] == ["date=2026-10-09", "date=2026-10-13", "date=2026-10-14"]
    for day in ("2026-10-09", "2026-10-13", "2026-10-14"):  # SOFR, not the 4-week bill it falls back to
        assert set(table(lake, "curve_params", day)["funding_source"]) == {"SOFR"}


# --- South Africa -------------------------------------------------------------------------------

SA_ROWS = PROJECT / "data" / "sa"


def seed_sa(raw, lake):
    """The committed Treasury rows into the lake with the loader's writers, the two series through
    the loader, as run_local.py does."""
    from bond_analytics import schemas

    bonds = schemas.from_csv("sa_bonds", (SA_ROWS / "nt_bonds.csv").read_text(encoding="utf-8"))
    for b in bonds:
        b["redemptions"] = [dt.date.fromisoformat(x) for x in b["redemptions"].split(",")]
    load.write_sa_bonds(lake, {b["bond_code"]: b for b in bonds})
    load.write_sa_auctions(lake, schemas.from_csv("sa_auctions", (SA_ROWS / "nt_auctions.csv").read_text(encoding="utf-8")),
                           rebuild=True)
    for path in SAMPLE.glob("raw/*/date=*/*.gz"):
        if path.parts[-3] in ("oecd", "imf"):
            raw.put(path.relative_to(SAMPLE).with_suffix("").as_posix(), gzip.decompress(path.read_bytes()))
    load.run(raw, lake, "full", sources=("oecd", "imf"))


@pytest.fixture(scope="module")
def sa(tmp_path_factory):
    root = tmp_path_factory.mktemp("sa")
    raw, lake = load.FolderStore(root / "raw"), load.FolderStore(root / "lake")
    seed_sa(raw, lake)
    return lake, app.run_sa(lake, "incremental")


def test_every_auction_since_2010_gets_a_curve_from_its_usable_bonds(sa):
    lake, summary = sa
    assert (summary["auction_dates"], summary["bonds_priced"], summary["bootstrap_failed"]) == (811, 2282, "none")
    assert len(summary["no_usable_bond"]) == 20 and "2025-04-22" not in summary["no_usable_bond"]
    prices = load.from_parquet(lake.get("sa_prices/year=2025/sa_prices.parquet"))
    assert "R2053" not in set(prices[prices["auction_date"].astype(str) == "2025-04-22"]["bond_code"])  # its 1.85
    assert set(prices["source"]) == {"National Treasury of the Republic of South Africa"}


def test_a_priced_auction_is_the_jse_formula_on_the_clearing_yield(sa):
    from bond_analytics import jse

    lake, _ = sa
    prices = load.from_parquet(lake.get("sa_prices/year=2014/sa_prices.parquet"))
    r = prices[(prices["auction_date"].astype(str) == "2014-07-15") & (prices["bond_code"] == "R2044")].iloc[0]
    assert r["all_in"] == pytest.approx(100.83272) and r["cum"]
    bonds = load.read_sa_bonds(lake)
    p = jse.price(jse.bond_from_static(bonds["R2044"]), dt.date(2014, 7, 18), 9.065)
    assert (r["accrued"], r["clean"]) == (p.accrued, p.clean)


def test_the_ten_year_point_tracks_the_oecd_and_imf_series(sa):
    lake, summary = sa
    check = load.from_parquet(lake.get("sa_crosscheck/sa_crosscheck.parquet"))
    both = check.dropna(subset=["par_minus_oecd_bp", "par_minus_imf_bp"])
    assert len(both) == summary["oecd_months"] == summary["imf_months"] == 153
    assert summary["oecd_mean_bp"] == pytest.approx(-30.6, abs=0.05)
    assert summary["imf_mean_bp"] == pytest.approx(-32.6, abs=0.05)
    changes = both[["par_10y", "oecd_irlt"]].diff().dropna()
    assert changes.corr().iloc[0, 1] > 0.85


def test_a_second_run_has_no_new_auction_and_keeps_the_check(sa):
    lake, _ = sa
    again = app.run_sa(lake, "incremental")
    assert again["auction_dates"] == 0
    assert len(load.from_parquet(lake.get("sa_crosscheck/sa_crosscheck.parquet"))) == 196


class FakeTreasury:
    """The fetch module's South African surface: one month of auctions, one schedule, the two series."""

    def __init__(self):
        import io

        import openpyxl

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "SEPTEMBER"
        for row in [["Auction date", dt.datetime(2026, 9, 1)], ["Settlement date", dt.datetime(2026, 9, 4)],
                    ["Bonds auctioned", "R2030", "R2040", "R2053"], ["Clearing yield", 8.1, 9.4, 10.2],
                    ["Best bid", 8.0, 9.3, 10.1], ["Worst bid", 8.2, 9.5, 10.3]]:
            ws.append(row)
        buf = io.BytesIO()
        wb.save(buf)
        self.xlsx = buf.getvalue()

    def fetch_nt_auction_history(self, pause_seconds):
        return {"fixed-rate-bonds-2026-27.xlsx": self.xlsx}

    def nt_schedules_listed(self):
        return {dt.date(2026, 3, 31): "a.pdf", dt.date(2026, 9, 30): "b.pdf", dt.date(2026, 8, 31): "c.pdf"}

    def fetch_nt_schedule_as_at(self, name):
        return b"%PDF " + name.encode()

    def fetch_oecd(self, day):
        return b"STRUCTURE,REF_AREA,MEASURE,TIME_PERIOD,OBS_VALUE\nDATAFLOW,ZAF,IRLT,2026-09,8.947\n"

    def fetch_imf(self, day):
        return b"observation_date,INTGSBZAM193N\n2026-09-01,8.97\n"


def test_the_sa_backfill_fetches_loads_and_builds_the_curves(tmp_path, monkeypatch):
    from bond_analytics import nt_schedule

    text = ("AS AT 30 SEPTEMBER 2026\nR2030 8.00% 04-Oct-13 95.261%   300 000 000 28/02 & 31/08 31-Jan-30 297 978 110 031\n"
            "R2040 9.00% 11-Sep-15 99.747%   250 000 000 31/01 & 31/07 31-Jan-40 335 282 614 746\n"
            "R2053 11.625% 11-Apr-23 99.780%  1 300 000 000 31/03 & 30/09 31-Mar-53 118 877 382 543\n")
    monkeypatch.setattr(nt_schedule, "pdf_text", lambda body: text)
    monkeypatch.setattr(app.time, "sleep", lambda s: None)
    raw, lake = load.FolderStore(tmp_path / "raw"), load.FolderStore(tmp_path / "lake")
    summary = app.backfill_sa(raw, lake, dt.date(2026, 11, 3), fetcher=FakeTreasury())
    assert summary["schedules"] == 2  # March and the latest, not every month
    assert (summary["auction_dates"], summary["bonds_priced"], summary["oecd_months"]) == (1, 3, 1.0)
    assert sorted(k.split("/")[2] for k in raw.list("raw/nt_schedule/")) == ["date=2026-03-31", "date=2026-09-30"]
    curve = load.from_parquet(lake.get("sa_curves/year=2026/sa_curves.parquet"))
    assert len(curve) == len(app.sa_curve.GRID)
