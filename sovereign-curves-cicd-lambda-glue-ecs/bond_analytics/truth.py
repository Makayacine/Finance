"""Price securities off a curve and compare with the market, by bucket, type and run status.

The error is model minus market: in price per 100 of the dirty price, and in basis points of
semiannual yield, the market's yield less the model price's yield (so a positive basis-point
error is a bond that trades cheap to the curve).
"""

from __future__ import annotations

from typing import List, Sequence

import pandas as pd

from .market import Quote
from .svensson import SvenssonCurve

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
