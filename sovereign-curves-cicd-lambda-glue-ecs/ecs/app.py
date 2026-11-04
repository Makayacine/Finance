"""ECS task: the curve and risk engine over the lake, and the one-off backfill.

Run by an EventBridge rule each time the Glue load succeeds (``--mode daily``): every price date
in ``prices/`` after the engine's watermark gets, in turn,

- **FIA's bootstrap**, one bond per maturity bucket (``bond_analytics.bootstrap``);
- **a Svensson fit** to off-the-run notes and bonds, GSW's way, warm-started from the day before;
- **fit errors** of every bill, note and bond against three curves: the bootstrap, our Svensson
  and, where the file has the date, GSW's;
- **risk** per security: DV01, modified duration and convexity on its own yield, key-rate DV01s
  at 2, 5, 10, 20 and 30 years on the fitted curve, and 30-day carry against the funding rate:
  SOFR from April 2018, the 4-week bill before.

``--mode backfill --start 2008-09-02 --end <date>`` is the same image doing what a Lambda's 15
minutes cannot: it fetches FedInvest one date at a time with a pause between them, and the whole
auction history, GSW's file and the New York Fed's rates for the range once each; it loads the
raw zone with the Glue job's own code, then runs the engine over the range.

Tables, one Parquet file per price date:

    curves/date=D/curves.parquet            zero and par yields on a tenor grid, per curve
    curve_params/date=D/curve_params.parquet Svensson parameters, fit size, RMSE, any bound hit
    fit_errors/date=D/fit_errors.parquet    model less market, per security and curve
    risk/date=D/risk.parquet                DV01, duration, convexity, key rates, carry

South Africa runs in the same task, after the United States, over the National Treasury's
auction dates after its own watermark (``bond_analytics.sa_curve``): each auction's cleared bonds
priced with the JSE formula and bootstrapped with FIA, then the monthly 10-year check against the
OECD and IMF series. One file per year, since auctions are weekly:

    sa_prices/year=Y/sa_prices.parquet      all-in, accrued, clean, DV01, duration, convexity
    sa_curves/year=Y/sa_curves.parquet      zero and par yields per auction date, extrapolation marked
    sa_crosscheck/sa_crosscheck.parquet     the 10-year par yield by month against OECD and IMF

``--market us`` or ``--market sa`` runs one; the default runs both.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import io
import logging
import math
import os
import sys
import time
import warnings
from pathlib import Path
from typing import Dict, List, Optional

warnings.simplefilter("ignore", SyntaxWarning)  # FIA.py compares a string with 'is not'

HERE = Path(__file__).resolve().parent
for p in (HERE, HERE.parent, HERE.parent / "glue-pythonshell", HERE.parent / "lambda-functions"):
    if (p / "bond_analytics").exists() or p.name in ("glue-pythonshell", "lambda-functions"):
        sys.path.insert(0, str(p))

import pandas as pd  # noqa: E402

import load  # noqa: E402
from bond_analytics import bootstrap, dates, market, risk, sa_curve, svensson, truth  # noqa: E402
from bond_analytics.dates import Security  # noqa: E402
from bond_analytics.securities import run_ranks  # noqa: E402

log = logging.getLogger("engine")

GRID = (0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 7.0, 10.0, 15.0, 20.0, 25.0, 30.0)
CURVE_KINDS = ("bill", "note", "bond")
SOFR_START = dt.date(2018, 4, 2)
CARRY_DAYS = 30


# --- reading the lake -------------------------------------------------------------------------


def price_dates(lake) -> List[dt.date]:
    out = set()
    for k in lake.list("prices/"):
        part = k.split("/")[1]
        if part.startswith("date="):
            out.add(dt.date.fromisoformat(part[5:]))
    return sorted(out)


def read_table_years(lake, table: str, name: str) -> pd.DataFrame:
    frames = [load.from_parquet(lake.get(k)) for k in lake.list(table + "/") if k.endswith(name)]
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    df["date"] = df["date"].map(lambda v: v if isinstance(v, dt.date) else pd.Timestamp(v).date())
    return df


def read_gsw(lake) -> Dict[dt.date, tuple]:
    df = read_table_years(lake, "reference_curves", "gsw.parquet")
    out = {}
    for rec in df.to_dict("records"):
        tau2 = rec["tau2"] if rec["tau2"] == rec["tau2"] else None
        out[rec["date"]] = (rec["beta0"], rec["beta1"], rec["beta2"], rec["beta3"] or 0.0, rec["tau1"], tau2)
    return out


def read_sofr(lake) -> Dict[dt.date, float]:
    df = read_table_years(lake, "funding", "rates.parquet")
    if df.empty:
        return {}
    df = df[df["rate_type"] == "SOFR"]
    return dict(zip(df["date"], df["percent_rate"]))


def _date(v) -> Optional[dt.date]:
    if v is None or v != v:
        return None
    return v if isinstance(v, dt.date) else pd.Timestamp(v).date()


def quotes_for_day(prices: pd.DataFrame, static: Dict[str, dict], day: dt.date) -> List[market.Quote]:
    """The day's bills, notes and bonds that have a price and have not matured, as FIA bonds."""
    settle = dates.settlement_date(day)
    ranks = run_ranks(static, settle)
    out = []
    for r in prices.to_dict("records"):
        if r["kind"] not in CURVE_KINDS or r["dirty"] is None or r["dirty"] != r["dirty"]:
            continue
        st = static.get(r["cusip"], {})
        sec = Security(
            cusip=r["cusip"], kind=r["kind"], coupon=float(r["coupon"]), maturity=_date(r["maturity"]),
            dated_date=st.get("dated_date"), first_coupon_date=st.get("first_coupon_date"),
            first_period=st.get("first_period"), issue_date=st.get("issue_date"), original_term=st.get("original_term"),
        )
        if sec.maturity <= settle:
            continue
        bond = dates.to_relative_bond(sec, settle, float(r["clean"]))
        out.append(market.Quote(sec, day, settle, float(r["clean"]), float(r["accrued"]), float(r["dirty"]),
                                bool(r["callable"]), ranks.get(sec.cusip), dates.year_fraction(settle, sec.maturity), bond))
    return out


# --- one day ----------------------------------------------------------------------------------


def funding_rate(day: dt.date, quotes: List[market.Quote], sofr: Dict[dt.date, float]):
    """(rate in percent, act/360, source). SOFR on or up to five days before the date from its
    start; before it, the money-market yield of the bill nearest four weeks from settlement."""
    if day >= SOFR_START:
        for back in range(0, 6):
            d = day - dt.timedelta(days=back)
            if d in sofr:
                return sofr[d], "SOFR"
    bills = [q for q in quotes if q.security.kind == "bill"]
    if not bills:
        return None, None
    b = min(bills, key=lambda q: abs((q.security.maturity - q.settle).days - 28))
    days = (b.security.maturity - b.settle).days
    return (100.0 / b.dirty - 1.0) * 360.0 / days * 100.0, "4-week bill"


def carry(q: market.Quote, rate: Optional[float]) -> Optional[float]:
    """Coupon income over the next 30 days less the cost of financing the dirty price, per 100.

    Price changes are left out: this is the funding leg, not a forecast of the return.
    """
    if rate is None or q.security.kind == "bill":
        return None
    horizon = q.settle + dt.timedelta(days=CARRY_DAYS)
    if horizon >= q.security.maturity:
        return None
    paid = sum(a for d, a in dates.cash_flows(q.security, q.settle) if d <= horizon)
    income = dates.accrued_interest(q.security, horizon) + paid - q.accrued
    return income - q.dirty * rate / 100.0 * CARRY_DAYS / 360.0


def engine_day(day: dt.date, quotes: List[market.Quote], gsw_params: Optional[tuple], sofr, warm: Optional[tuple]):
    """Every table's rows for one price date, and the Svensson parameters to warm-start the next."""
    fit_set = market.fit_sample(quotes)
    t0 = time.perf_counter()
    fit = svensson.fit([q.bond for q in fit_set], start=warm)
    fit_seconds = time.perf_counter() - t0

    picked = bootstrap.select(market.bootstrap_candidates(quotes))
    sink = io.StringIO()
    t0 = time.perf_counter()
    with contextlib.redirect_stdout(sink):  # FIA prints when it extrapolates past the last knot
        boot = bootstrap.build(picked)
    boot_seconds = time.perf_counter() - t0
    rate, source = funding_rate(day, quotes, sofr)

    curves = {"svensson": svensson.SvenssonCurve(fit.params), "fia_bootstrap": boot}
    if gsw_params is not None:
        curves["gsw"] = svensson.SvenssonCurve(gsw_params)

    grid = []
    with contextlib.redirect_stdout(sink):
        for name, curve in curves.items():
            for t in GRID:
                df = curve.discount_factor(t)
                coupons = [curve.discount_factor(s) for s in [t - 0.5 * k for k in range(int(t * 2))] if s > 0]
                par = 200.0 * (1.0 - df) / sum(coupons) if t >= 0.5 else None
                grid.append({"price_date": day, "curve": name, "tenor": t, "zero_rate": -math.log(df) / t * 100.0,
                             "par_yield": par, "discount_factor": df})

    params = [{"price_date": day, "curve": "svensson", **dict(zip(svensson.PARAM_NAMES, fit.params)),
               "rmse_bp": fit.rmse_bp, "n_bonds": fit.n_bonds, "params_at_bound": ",".join(fit.at_bound),
               "seconds": fit_seconds, "funding_rate": rate, "funding_source": source}]
    if gsw_params is not None:
        params.append({"price_date": day, "curve": "gsw", **dict(zip(svensson.PARAM_NAMES, gsw_params)),
                       "rmse_bp": None, "n_bonds": None, "params_at_bound": "", "seconds": None,
                       "funding_rate": rate, "funding_source": source})
    params.append({"price_date": day, "curve": "fia_bootstrap", "rmse_bp": 0.0, "n_bonds": len(picked),
                   "params_at_bound": "", "seconds": boot_seconds, "funding_rate": rate, "funding_source": source})

    in_fit = {q.security.cusip for q in fit_set}
    in_boot = {c.cusip for c in picked}
    errors = []
    with contextlib.redirect_stdout(sink):
        for name, curve in curves.items():
            e = truth.price_errors(quotes, curve)
            e["curve"] = name
            e["in_fit"] = e["cusip"].isin(in_fit if name != "fia_bootstrap" else in_boot)
            e["params_at_bound"] = ",".join(fit.at_bound) if name == "svensson" else ""
            errors.append(e)
    errors = pd.concat(errors, ignore_index=True)
    errors["bucket"] = errors["bucket"].astype(str)

    risks = []
    for q in quotes:
        m = risk.measure(q.bond, fit.params)
        row = {"price_date": day, "cusip": q.security.cusip, "kind": q.security.kind, "coupon": q.security.coupon,
               "maturity": q.security.maturity, "years": q.years, "run": q.run, "dirty": q.dirty, "ytm": m.ytm,
               "dv01": m.dv01, "modified_duration": m.modified_duration, "convexity": m.convexity,
               "z_spread_bp": m.z_spread_bp, "curve_dv01": m.curve_dv01, "carry_30d": carry(q, rate)}
        for tenor, v in m.key_rate_dv01.items():
            row[f"krd_{int(tenor)}y"] = v
        risks.append(row)
    return {
        "curves": pd.DataFrame(grid),
        "curve_params": pd.DataFrame(params),
        "fit_errors": errors,
        "risk": pd.DataFrame(risks),
    }, fit.params


TABLES = ("curves", "curve_params", "fit_errors", "risk")


def run(lake, load_type: str = "incremental", start: Optional[dt.date] = None, end: Optional[dt.date] = None) -> dict:
    """Run the engine over price dates in the lake: all of them (``full``), or those after its watermark."""
    days = [d for d in price_dates(lake) if (start is None or d >= start) and (end is None or d <= end)]
    mark = load.read_watermarks(lake).get("engine", {})
    warm = None
    if load_type == "incremental" and mark.get("loaded_through"):
        days = [d for d in days if d > dt.date.fromisoformat(mark["loaded_through"])]
        warm = tuple(mark["svensson"]) if mark.get("svensson") else None
    static = load.read_static(lake)
    gsw_by_date = read_gsw(lake)
    sofr = read_sofr(lake)
    bounded, unpriced = [], []
    t0 = time.perf_counter()
    for day in days:
        prices = load.from_parquet(lake.get(load.key("prices", f"date={day.isoformat()}", "prices.parquet")))
        quotes = quotes_for_day(prices, static, day)
        if not quotes:  # FedInvest's end-of-day column is zero on every row of 12 September and 21 November 2014
            unpriced.append(day.isoformat())
            log.warning("%s: no security has a price; no curve for this date", day)
            continue
        tables, warm = engine_day(day, quotes, gsw_by_date.get(day), sofr, warm)
        for name in TABLES:
            lake.put(load.key(name, f"date={day.isoformat()}", f"{name}.parquet"), load.to_parquet(tables[name], name))
        at = tables["curve_params"].iloc[0]["params_at_bound"]
        if at:
            bounded.append(f"{day}:{at}")
            log.info("%s: Svensson fit ended on a bound: %s", day, at)
        load.write_watermark(lake, "engine", day, svensson=list(warm))
        log.info("%s: %d securities", day, len(quotes))
    if bounded:
        log.warning("%d of %d Svensson fits ended on a bound; each is in curve_params and fit_errors", len(bounded), len(days))
    return {"dates": len(days), "seconds": round(time.perf_counter() - t0, 1), "fits_on_a_bound": bounded or "none",
            "unpriced": unpriced or "none"}


# --- South Africa -----------------------------------------------------------------------------


def read_sa_auctions(lake) -> pd.DataFrame:
    return pd.DataFrame(load.read_sa_auctions(lake), columns=load.schemas.columns("sa_auctions"))


def run_sa(lake, load_type: str = "incremental", start: Optional[dt.date] = None, end: Optional[dt.date] = None) -> dict:
    """Curves for the auction dates in the lake: all of them (``full``), or those after the SA watermark."""
    auctions = read_sa_auctions(lake)
    bonds = load.read_sa_bonds(lake)
    days = sorted({d for d in auctions["auction_date"] if (start is None or d >= start) and (end is None or d <= end)})
    mark = load.read_watermarks(lake).get("engine_sa", {})
    if load_type == "incremental" and mark.get("loaded_through"):
        days = [d for d in days if d > dt.date.fromisoformat(mark["loaded_through"])]
    elif load_type == "full":
        lake.delete_prefix("sa_prices/")
        lake.delete_prefix("sa_curves/")
    t0 = time.perf_counter()
    prices, grid, skipped, failed = [], [], [], []
    by_day = {d: g.to_dict("records") for d, g in auctions.groupby("auction_date")}
    for day in days:
        try:
            p, c = sa_curve.day_tables(day, by_day[day], bonds)
        except (ZeroDivisionError, ValueError, OverflowError) as exc:  # FIA's Newton step, or two settlements
            failed.append(f"{day}:{type(exc).__name__}")
            log.warning("%s: no curve: %s: %s", day, type(exc).__name__, exc)
            continue
        if not c:
            skipped.append(day.isoformat())
        prices.extend(p)
        grid.extend(c)
    if prices:
        load.upsert_by_year(lake, "sa_prices", "sa_prices.parquet", pd.DataFrame(prices),
                            ["auction_date", "bond_code"], date_col="auction_date")
        load.upsert_by_year(lake, "sa_curves", "sa_curves.parquet", pd.DataFrame(grid),
                            ["auction_date", "tenor"], date_col="auction_date")
    curves = read_table_years_by(lake, "sa_curves", "sa_curves.parquet", "auction_date")
    reference = read_table_years(lake, "sa_reference", "sa_reference.parquet")
    check = sa_curve.crosscheck(curves, reference) if len(curves) else pd.DataFrame(columns=load.schemas.columns("sa_crosscheck"))
    lake.put("sa_crosscheck/sa_crosscheck.parquet", load.to_parquet(check, "sa_crosscheck"))
    if days:
        load.write_watermark(lake, "engine_sa", days[-1])
    if skipped:
        log.warning("%d auction dates had no bond whose yield passed its checks: %s", len(skipped), ", ".join(skipped))
    out = {"auction_dates": len(days), "bonds_priced": len(prices), "seconds": round(time.perf_counter() - t0, 1),
           "no_usable_bond": skipped or "none", "bootstrap_failed": failed or "none"}
    out.update({k: (round(v, 1) if isinstance(v, float) else v) for k, v in sa_curve.summary(check).items()})
    return out


def read_table_years_by(lake, table: str, name: str, date_col: str) -> pd.DataFrame:
    frames = [load.from_parquet(lake.get(k)) for k in lake.list(table + "/") if k.endswith(name)]
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    df[date_col] = df[date_col].map(_date)
    return df


def backfill_sa(raw, lake, end: dt.date, pause_seconds: float = 2.0, fetcher=None) -> dict:
    """Every fiscal year's auction file, one debt schedule a year from March 2011 and the latest,
    and the two monthly series; then the load and the SA engine over all of it."""
    import fetch

    fetcher = fetcher or fetch
    for name, body in fetcher.fetch_nt_auction_history(pause_seconds=pause_seconds).items():
        raw.put(fetch.raw_key("nt_auctions", end, name), body)
    listed = fetcher.nt_schedules_listed()
    wanted = sorted({d for d in listed if d.month == 3 and d.day == 31 and d.year >= 2011} | {max(listed)})
    for i, d in enumerate(wanted):
        if i:
            time.sleep(pause_seconds)
        raw.put(fetch.raw_key("nt_schedule", d, "schedule.pdf"), fetcher.fetch_nt_schedule_as_at(listed[d]))
    raw.put(fetch.raw_key("oecd", end, "irlt.csv"), fetcher.fetch_oecd(end))
    raw.put(fetch.raw_key("imf", end, "INTGSBZAM193N.csv"), fetcher.fetch_imf(end))
    load.run(raw, lake, "full", date_filter=end, sources=("nt_schedule", "nt_auctions", "oecd", "imf"))
    summary = run_sa(lake, "full")
    summary["schedules"] = len(wanted)
    return summary


# --- backfill ---------------------------------------------------------------------------------


def backfill(raw, lake, start: dt.date, end: dt.date, pause_seconds: float = 2.0, fetcher=None) -> dict:
    """Fetch every business day in the range, load it, then run the engine over it."""
    import fetch

    fetcher = fetcher or fetch
    days = []
    d = start
    while d <= end:
        if dates.is_business_day(d):
            days.append(d)
        d += dt.timedelta(days=1)
    status = fetcher.backfill(days, raw.put, pause_seconds=pause_seconds, sources=("fedinvest",))
    fetched = {d for d, s in status.items() if s["fedinvest"].startswith("ok")}
    history = fetcher.fetch_auction_history()
    raw.put(fetch.raw_key("auctions", end, "auctions.json"), history)
    raw.put(fetch.raw_key("gsw", end, "feds200628.csv"), fetcher.fetch_gsw(end))
    raw.put(fetch.raw_key("nyfed", end, "rates.json"), fetcher.fetch_nyfed(end, start=start))
    load.run(raw, lake, "incremental", date_filter=end, sources=("auctions", "fedinvest", "gsw", "nyfed"))
    summary = run(lake, "full", start, end)
    summary["fetched"] = f"{len(fetched)} of {len(days)} business days"
    return summary


def main(argv=None) -> dict:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    ap = argparse.ArgumentParser(description="Curve and risk engine")
    ap.add_argument("--mode", choices=("daily", "full", "backfill"), default="daily")
    ap.add_argument("--start", type=dt.date.fromisoformat)
    ap.add_argument("--end", type=dt.date.fromisoformat)
    ap.add_argument("--lake-bucket", default=os.environ.get("LAKE_BUCKET"))
    ap.add_argument("--raw-bucket", default=os.environ.get("RAW_BUCKET"))
    ap.add_argument("--local-root", help="folders standing in for the buckets, as run_local.py uses")
    ap.add_argument("--market", choices=("us", "sa", "both"), default="both")
    args = ap.parse_args(argv)
    if args.local_root:
        raw = load.FolderStore(Path(args.local_root) / "raw-bucket")
        lake = load.FolderStore(Path(args.local_root) / "lake-bucket")
    else:
        raw, lake = load.S3Store(args.raw_bucket) if args.raw_bucket else None, load.S3Store(args.lake_bucket)
    summary = {}
    if args.mode == "backfill":
        if not (args.end and raw is not None) or (args.market != "sa" and not args.start):
            ap.error("backfill needs --end, a raw bucket and, for the United States, --start")
        if args.market in ("us", "both"):
            summary["us"] = backfill(raw, lake, args.start, args.end)
        if args.market in ("sa", "both"):
            summary["sa"] = backfill_sa(raw, lake, args.end)
    else:
        load_type = "full" if args.mode == "full" else "incremental"
        if args.market in ("us", "both"):
            summary["us"] = run(lake, load_type, args.start, args.end)
        if args.market in ("sa", "both"):
            summary["sa"] = run_sa(lake, load_type, args.start, args.end)
    log.info("engine: %s", summary)
    return summary


if __name__ == "__main__":
    main()
