"""Price every Treasury note and bond off the Fed's GSW curve and compare with FedInvest.

For each date: FedInvest's clean end-of-day price plus accrued interest at T+1 settlement is
the market's dirty price; the GSW parameters for the same date, through ``SvenssonCurve`` and
FIA's ``relative_bond.price``, give the model's. The tables break the error down by maturity
bucket and security type, and by run status, in price per 100 and in basis points of yield.

Inputs are read from ``data/raw/`` laid out as the S3 raw zone; ``--fetch`` downloads whatever
is missing (three FedInvest POSTs, the GSW file and the auction history).

    python local-development/truth_check.py --fetch
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import warnings
from pathlib import Path

warnings.simplefilter("ignore", SyntaxWarning)  # FIA.py compares a string with 'is not'

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "lambda-functions"))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from bond_analytics import gsw, market, truth  # noqa: E402
from bond_analytics.fedinvest import parse_prices  # noqa: E402
from bond_analytics.securities import static_by_cusip  # noqa: E402

DATES = ("2008-09-15", "2020-03-16", "2026-10-23")


def _raw(root: Path, source: str, day: str, name: str) -> Path:
    return root / source / f"date={day}" / name


def ensure_inputs(root: Path, dates, gsw_date: str, fetch: bool) -> None:
    import fetch as fetcher

    wanted = [(_raw(root, "fedinvest", d, "prices.html"), lambda d=d: fetcher.fetch_fedinvest(dt.date.fromisoformat(d))) for d in dates]
    wanted.append((_raw(root, "gsw", gsw_date, "feds200628.csv"), lambda: fetcher.fetch_gsw(dt.date.fromisoformat(gsw_date))))
    wanted.append((root / "auctions" / "history" / "auctions.json", fetcher.fetch_auction_history))
    for path, get in wanted:
        if path.exists():
            continue
        if not fetch:
            raise SystemExit(f"missing {path.relative_to(PROJECT).as_posix()}; run with --fetch")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(get())


def run(root: Path, dates, gsw_date: str) -> pd.DataFrame:
    static = static_by_cusip(json.loads((root / "auctions" / "history" / "auctions.json").read_bytes())["data"])
    curves = {r["date"]: r for r in gsw.read_rows(_raw(root, "gsw", gsw_date, "feds200628.csv").read_text())}
    frames = []
    for d in dates:
        day = dt.date.fromisoformat(d)
        html = _raw(root, "fedinvest", d, "prices.html").read_text(encoding="latin-1")
        quotes = market.build_quotes(parse_prices(html, day), static, day)
        frames.append(truth.price_errors(quotes, gsw.params_of(curves[day])))
    return pd.concat(frames, ignore_index=True)


def report(df: pd.DataFrame) -> str:
    out = []
    curve = df[~df.callable]
    for d, day in curve.groupby("price_date"):
        out.append(f"\n{d}: {len(day)} securities, {int(df[(df.price_date == d) & df.callable].shape[0])} callable left out\n")
        out.append(truth.summarise(day, ["bucket", "kind"]).to_markdown())
    out.append("\nBy run status, notes and bonds\n")
    nb = curve[curve.kind != "bill"]
    out.append(truth.summarise(nb, ["price_date", "run"]).to_markdown())
    out.append("\nChecks, notes and bonds: price error regressed on the share of the coupon period")
    out.append("elapsed (a wrong accrued shows as a slope near half a coupon) and on maturity\n")
    rows = []
    for d, day in nb[nb.coupon > 0].groupby("price_date"):
        elapsed = day.accrued / (day.coupon / 2)
        rows.append({"price_date": d, "n": len(day), "max_abs_price": round(day.error_price.abs().max(), 3),
                     "median_half_coupon": round(float((day.coupon / 2).median()), 3),
                     "slope_per_period_elapsed": round(float(np.polyfit(elapsed, day.error_price, 1)[0]), 3),
                     "slope_per_10y_maturity": round(float(np.polyfit(day.years, day.error_price, 1)[0]) * 10, 3)})
    out.append(pd.DataFrame(rows).to_markdown(index=False))
    return "\n".join(out)


def fit_and_bootstrap(root: Path, dates, gsw_date: str) -> str:
    """Our own Svensson fit against GSW's, and FIA's bootstrap timed, on the same dates."""
    import contextlib
    import io
    import time

    from bond_analytics import bootstrap, svensson

    static = static_by_cusip(json.loads((root / "auctions" / "history" / "auctions.json").read_bytes())["data"])
    curves = {r["date"]: r for r in gsw.read_rows(_raw(root, "gsw", gsw_date, "feds200628.csv").read_text())}
    tenors = (0.5, 1, 2, 5, 10, 20, 30)
    fits, boots = [], []
    for d in dates:
        day = dt.date.fromisoformat(d)
        html = _raw(root, "fedinvest", d, "prices.html").read_text(encoding="latin-1")
        quotes = market.build_quotes(parse_prices(html, day), static, day)
        ref = gsw.params_of(curves[day])
        t0 = time.perf_counter()
        fit = svensson.fit([q.bond for q in market.fit_sample(quotes)])
        fit_s = time.perf_counter() - t0
        row = {"price_date": d, "bonds": fit.n_bonds, "seconds": round(fit_s, 2), "rmse_bp": round(fit.rmse_bp, 2)}
        for t in tenors:
            row[f"{t:g}y"] = round(float(svensson.zero_rate(t, fit.params) - svensson.zero_rate(t, ref)) * 100, 1)
        fits.append(row)
        picked = bootstrap.select(market.bootstrap_candidates(quotes))
        t0 = time.perf_counter()
        with contextlib.redirect_stdout(io.StringIO()):  # FIA prints when it extrapolates past 30y
            curve = bootstrap.build(picked)
            boot_s = time.perf_counter() - t0
            row = {"price_date": d, "bonds": len(picked), "seconds": round(boot_s, 3)}
            for t in tenors:
                row[f"{t:g}y"] = round((curve.get_yield(t) - float(svensson.zero_rate(t, ref))) * 100, 1)
        boots.append(row)
    return "\n".join([
        "\nOur Svensson fit minus GSW, zero rates in basis points\n",
        pd.DataFrame(fits).to_markdown(index=False),
        "\nFIA's bootstrap minus GSW, zero rates in basis points\n",
        pd.DataFrame(boots).to_markdown(index=False),
    ])


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--raw", type=Path, default=PROJECT / "data" / "raw")
    ap.add_argument("--dates", nargs="+", default=list(DATES))
    ap.add_argument("--gsw-date", default="2026-11-02", help="the date the GSW file was fetched")
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--out", type=Path, default=PROJECT / "_localrun" / "truth_check.csv")
    args = ap.parse_args(argv)
    ensure_inputs(args.raw, args.dates, args.gsw_date, args.fetch)
    df = run(args.raw, args.dates, args.gsw_date)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(args.out, index=False)
    print(report(df))
    print(fit_and_bootstrap(args.raw, args.dates, args.gsw_date))


if __name__ == "__main__":
    main()
