"""Price securities off a curve and compare with the market, by bucket, type and run status.

The error is model minus market: in price per 100 of the dirty price, and in basis points of
semiannual yield, the market's yield less the model price's yield (so a positive basis-point
error is a bond that trades cheap to the curve).
"""

from __future__ import annotations

from typing import List, Sequence

import numpy as np
import pandas as pd

from .market import Quote
from .svensson import SvenssonCurve

MIN_YEARS = 0.25  # GSW's rule: nothing within three months of maturity

MATURITY_BUCKETS: Sequence[float] = (0.0, 1.0, 2.0, 3.0, 5.0, 7.0, 10.0, 15.0, 20.0, 25.0, 31.0)
BUCKET_LABELS = ["0-1y", "1-2y", "2-3y", "3-5y", "5-7y", "7-10y", "10-15y", "15-20y", "20-25y", "25-30y"]


def price_errors(quotes: List[Quote], params: Sequence[float]) -> pd.DataFrame:
    curve = SvenssonCurve(params)
    rows = []
    for q in quotes:
        model = q.bond.price(curve)
        y_mkt = q.bond.YTM(q.dirty, compounding=2)
        y_mod = q.bond.YTM(model, compounding=2)
        rows.append(
            {
                "price_date": q.price_date,
                "cusip": q.security.cusip,
                "kind": q.security.kind,
                "coupon": q.security.coupon,
                "maturity": q.security.maturity,
                "years": q.years,
                "original_term": q.security.original_term,
                "issue_date": q.security.issue_date,
                "run": q.run,
                "callable": q.callable,
                "clean": q.clean,
                "accrued": q.accrued,
                "dirty": q.dirty,
                "model_dirty": model,
                "error_price": model - q.dirty,
                "ytm": y_mkt,
                "error_bp": (y_mkt - y_mod) * 100.0,
            }
        )
    df = pd.DataFrame(rows)
    df["bucket"] = pd.cut(df["years"], MATURITY_BUCKETS, labels=BUCKET_LABELS, right=False)
    return df


def summarise(df: pd.DataFrame, by: List[str]) -> pd.DataFrame:
    """Count, mean and mean absolute error in price and in basis points, per group."""
    g = df.groupby(by, observed=True)
    out = pd.DataFrame(
        {
            "n": g.size(),
            "mean_price": g["error_price"].mean(),
            "mae_price": g["error_price"].apply(lambda s: s.abs().mean()),
            "mean_bp": g["error_bp"].mean(),
            "mae_bp": g["error_bp"].apply(lambda s: s.abs().mean()),
        }
    )
    return out.round({"mean_price": 3, "mae_price": 3, "mean_bp": 1, "mae_bp": 1})


def table_rows(df: pd.DataFrame) -> pd.DataFrame:
    """The rows every error table uses: GSW's three-month rule, and no callable bonds."""
    return df[(df["years"] >= MIN_YEARS) & ~df["callable"]]


def run_status_within_buckets(df: pd.DataFrame) -> pd.DataFrame:
    """Mean absolute error in bp by run status, inside each bucket that holds an on-the-run issue.

    Pooled over all maturities the comparison says the opposite of what is true, because every
    note under a year is off-the-run and short yields are noisy; within a bucket like is compared
    with like.
    """
    nb = df[df["kind"].isin(["note", "bond"])]
    with_otr = nb[nb["run"] == "on-the-run"][["price_date", "bucket"]].drop_duplicates()
    nb = nb.merge(with_otr, on=["price_date", "bucket"])
    t = nb.pivot_table(index=["price_date", "bucket"], columns="run", values="error_bp",
                       aggfunc=lambda s: s.abs().mean(), observed=True)
    n = nb.pivot_table(index=["price_date", "bucket"], columns="run", values="error_bp", aggfunc="size", observed=True)
    order = [c for c in ("on-the-run", "first off-the-run", "off-the-run") if c in t.columns]
    t = t[order].round(1)
    t["n off-the-run"] = n.get("off-the-run")
    return t


def coupon_slope(df: pd.DataFrame, value: str = "error_bp", controls=("bucket",), extra=()) -> dict:
    """OLS slope of the error on the coupon, within maturity buckets, on-the-run issues left out.

    Bucket dummies absorb any level difference between buckets, so the slope comes only from
    bonds and notes that share a bucket. ``extra`` adds numeric controls, such as the years since
    issue, to tell a coupon effect from an age effect.
    """
    d = df[df["kind"].isin(["note", "bond"]) & (df["run"] != "on-the-run")].dropna(subset=[value, *extra])
    dummies = pd.get_dummies(d[list(controls)].astype(str), drop_first=False, dtype=float)
    X = np.column_stack([d["coupon"].to_numpy(float)] + [d[c].to_numpy(float) for c in extra] + [dummies.to_numpy()])
    y = d[value].to_numpy(float)
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ beta
    dof = max(len(y) - np.linalg.matrix_rank(X), 1)
    cov = np.linalg.pinv(X.T @ X) * (resid @ resid) / dof
    out = {"n": len(y), "slope": float(beta[0]), "se": float(np.sqrt(cov[0, 0]))}
    for i, c in enumerate(extra, start=1):
        out[f"slope_{c}"] = float(beta[i])
        out[f"se_{c}"] = float(np.sqrt(cov[i, i]))
    return out
