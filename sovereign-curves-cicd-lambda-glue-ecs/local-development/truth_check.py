"""Price every Treasury note and bond off the Fed's GSW curve and compare with FedInvest.

For each date: FedInvest's clean end-of-day price plus accrued interest at T+1 settlement is
the market's dirty price; the GSW parameters for the same date, through ``SvenssonCurve`` and
FIA's ``relative_bond.price``, give the model's. The tables break the error down by maturity
bucket and security type, and by run status, in price per 100 and in basis points of yield.

Inputs are read from a folder laid out as the raw bucket (``raw/<source>/date=<D>/<file>``):
``data/full`` by default, where ``--fetch`` downloads whatever is missing (three FedInvest
POSTs, the GSW file and the auction history), or ``_localrun/raw-bucket`` after
``run_local.py`` has staged the committed sample, which holds the same three dates.

    python local-development/truth_check.py --fetch
    python local-development/truth_check.py --raw-root _localrun/raw-bucket
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
    return root / "raw" / source / f"date={day}" / name


def _newest(root: Path, source: str, name: str) -> Path:
    found = sorted((root / "raw" / source).glob(f"date=*/{name}"))
    if not found:
        raise SystemExit(f"no {source} file under {(root / 'raw' / source).as_posix()}")
    return found[-1]


def load_static(root: Path):
    records = []
    for path in sorted((root / "raw" / "auctions").glob("date=*/auctions.json")):
        records.extend(json.loads(path.read_bytes())["data"])
    return static_by_cusip(records)


def load_curves(root: Path):
    return {r["date"]: r for r in gsw.read_rows(_newest(root, "gsw", "feds200628.csv").read_text())}


def ensure_inputs(root: Path, dates, gsw_date: str, fetch: bool) -> None:
    import fetch as fetcher

    wanted = [(_raw(root, "fedinvest", d, "prices.html"), lambda d=d: fetcher.fetch_fedinvest(dt.date.fromisoformat(d))) for d in dates]
    wanted.append((_raw(root, "gsw", gsw_date, "feds200628.csv"), lambda: fetcher.fetch_gsw(dt.date.fromisoformat(gsw_date))))
    wanted.append((_raw(root, "auctions", gsw_date, "auctions.json"), fetcher.fetch_auction_history))
    for path, get in wanted:
        if path.exists():
            continue
        if not fetch:
            raise SystemExit(f"missing {path.relative_to(PROJECT).as_posix()}; run with --fetch")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(get())


def run(root: Path, dates) -> pd.DataFrame:
    static, curves = load_static(root), load_curves(root)
    frames = []
    for d in dates:
        day = dt.date.fromisoformat(d)
        html = _raw(root, "fedinvest", d, "prices.html").read_text(encoding="latin-1")
        quotes = market.build_quotes(parse_prices(html, day), static, day)
        frames.append(truth.price_errors(quotes, gsw.params_of(curves[day])))
    return pd.concat(frames, ignore_index=True)


def report(df: pd.DataFrame) -> str:
    out = []
    rows = truth.table_rows(df)
    for d, day in rows.groupby("price_date"):
        all_day = df[df.price_date == d]
        out.append(f"\n{d}: {len(day)} securities; left out: {int(all_day.callable.sum())} callable, "
                   f"{int((all_day.years < truth.MIN_YEARS).sum())} within three months of maturity\n")
        out.append(truth.summarise(day, ["bucket", "kind"]).to_markdown())
    nb = rows[rows.kind != "bill"]
    out.append("\nBy run status inside each bucket that holds an on-the-run issue: mean absolute error, bp\n")
    out.append(truth.run_status_within_buckets(nb).to_markdown())
    out.append("\nBy run status, notes and bonds a year or more from maturity\n")
    out.append(truth.summarise(nb[nb.years >= 1], ["price_date", "run"]).to_markdown())
    out.append("\nChecks, notes and bonds: price error regressed on the share of the coupon period")
    out.append("elapsed (a wrong accrued shows as a slope near half a coupon) and on maturity\n")
    checks = []
    for d, day in nb[nb.coupon > 0].groupby("price_date"):
        elapsed = day.accrued / (day.coupon / 2)
        checks.append({"price_date": d, "n": len(day), "max_abs_price": round(day.error_price.abs().max(), 3),
                       "median_half_coupon": round(float((day.coupon / 2).median()), 3),
                       "slope_per_period_elapsed": round(float(np.polyfit(elapsed, day.error_price, 1)[0]), 3),
                       "slope_per_10y_maturity": round(float(np.polyfit(day.years, day.error_price, 1)[0]) * 10, 3)})
    out.append(pd.DataFrame(checks).to_markdown(index=False))
    out.append("\nCoupon: error regressed on the coupon within maturity buckets, on-the-run issues left out;")
    out.append("then again with the years since issue beside it. Slopes per percentage point of coupon.\n")
    slopes = []
    for d, day in nb.groupby("price_date"):
        day = day.assign(age=[(pd.Timestamp(d) - pd.Timestamp(i)).days / 365.25 if i is not None and i == i else np.nan
                              for i in day.issue_date])
        bp = truth.coupon_slope(day, "error_bp")
        px = truth.coupon_slope(day, "error_price")
        both = truth.coupon_slope(day, "error_bp", extra=("age",))
        slopes.append({"price_date": d, "n": bp["n"], "bp per coupon point": f"{bp['slope']:+.2f} ({bp['se']:.2f})",
                       "price per coupon point": f"{px['slope']:+.3f} ({px['se']:.3f})",
                       "bp per coupon point, age held": f"{both['slope']:+.2f} ({both['se']:.2f})",
                       "bp per year of age": f"{both['slope_age']:+.2f} ({both['se_age']:.2f})"})
    out.append(pd.DataFrame(slopes).to_markdown(index=False))
    out.append("(standard errors in brackets)")
    return "\n".join(out)


def fit_and_bootstrap(root: Path, dates) -> str:
    """Our own Svensson fit against GSW's, and FIA's bootstrap timed, on the same dates."""
    import contextlib
    import io
    import time

    from bond_analytics import bootstrap, svensson

    static, curves = load_static(root), load_curves(root)
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
        row = {"price_date": d, "bonds": fit.n_bonds, "seconds": round(fit_s, 2), "rmse_bp": round(fit.rmse_bp, 2),
               "at_bound": ",".join(fit.at_bound) or "-"}
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
    ap.add_argument("--raw-root", type=Path, default=PROJECT / "data" / "full")
    ap.add_argument("--dates", nargs="+", default=list(DATES))
    ap.add_argument("--gsw-date", default="2026-11-02", help="the date to file a fetched GSW file and auction history under")
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--out", type=Path, default=PROJECT / "_localrun" / "truth_check.csv")
    args = ap.parse_args(argv)
    if args.fetch:
        ensure_inputs(args.raw_root, args.dates, args.gsw_date, args.fetch)
    df = run(args.raw_root, args.dates)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(args.out, index=False)
    print(report(df))
    print(fit_and_bootstrap(args.raw_root, args.dates))


if __name__ == "__main__":
    main()
