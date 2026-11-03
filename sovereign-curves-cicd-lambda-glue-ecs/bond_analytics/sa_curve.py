"""A South African curve per auction date, from that day's cleared bonds, and its check.

On an auction date the Treasury sells two to five fixed-rate bonds, and each clears at one
yield. Each yield becomes an all-in price through the JSE formula (``jse.price``) for the
auction's settlement date, and FIA's exact bootstrap (``build_from_bonds``) puts one knot at each
bond's maturity, so the curve reprices every bond it is built from. Beyond the shortest and the
longest bond FIA holds the zero rate flat; a tenor out there is marked ``extrapolated``.

Rows whose yield fails a check (``nt_auctions.usable``) are left out before any of this.

The check is monthly: the 10-year par yield, averaged over the month's auction dates on which
10 years lies between the shortest and the longest bond, against the OECD's long-term rate and
the IMF's government bond yield for South Africa.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import math
from typing import Dict, Iterable, List, Optional, Tuple

import pandas as pd

from . import FIA, jse, nt_auctions

GRID = (0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 7.0, 10.0, 15.0, 20.0, 25.0, 30.0)
CHECK_TENOR = 10.0


def price_rows(rows: Iterable[dict], bonds: Dict[str, dict]) -> List[dict]:
    """One ``sa_prices`` row per usable auction row: the JSE prices and the risk on the JSE yield."""
    out = []
    for r in rows:
        if not nt_auctions.usable(r):
            continue
        bond = jse.bond_from_static(bonds[r["bond_code"]])
        settle, y = r["settle_date"], float(r["clearing_yield"])
        p = jse.price(bond, settle, y)
        d01 = jse.dv01(bond, settle, y)
        mid = jse.price(bond, settle, y, rounded=False).all_in
        up = jse.price(bond, settle, y + 0.01, rounded=False).all_in
        down = jse.price(bond, settle, y - 0.01, rounded=False).all_in
        rb = jse.to_relative_bond(bond, settle, p.all_in)
        out.append({
            "auction_date": r["auction_date"], "settle_date": settle, "bond_code": bond.code, "coupon": bond.coupon,
            "maturity": bond.maturity, "years": jse.year_fraction(settle, bond.maturity), "clearing_yield": y,
            "cum": p.cum, "all_in": p.all_in, "accrued": p.accrued, "clean": p.clean, "dv01": d01,
            "modified_duration": d01 * 1e4 / mid, "convexity": (up + down - 2.0 * mid) / (mid * 1e-8),
            "fia_ytm": rb.YTM(p.all_in, compounding=2), "source": nt_auctions.SOURCE, "_bond": rb,
        })
    return out


def build(priced: List[dict]):
    """FIA's exact bootstrap over one auction's bonds. They must share a settlement date."""
    settles = {r["settle_date"] for r in priced}
    if len(settles) != 1:
        raise ValueError(f"one curve needs one settlement date, got {sorted(settles)}")
    sink = io.StringIO()
    with contextlib.redirect_stdout(sink):  # FIA prints when it extrapolates past the last knot
        return FIA.curve_factory(bondlist=[r["_bond"] for r in priced])


def curve_rows(day: dt.date, priced: List[dict], curve) -> List[dict]:
    knots = sorted(r["years"] for r in priced)
    out = []
    sink = io.StringIO()
    with contextlib.redirect_stdout(sink):
        for t in GRID:
            df = curve.discount_factor(t)
            coupons = [curve.discount_factor(s) for s in (t - 0.5 * k for k in range(int(t * 2))) if s > 0]
            par = 200.0 * (1.0 - df) / sum(coupons) if t >= 0.5 else None
            out.append({"auction_date": day, "tenor": t, "zero_rate": -math.log(df) / t * 100.0, "par_yield": par,
                        "discount_factor": df, "extrapolated": not (knots[0] <= t <= knots[-1]),
                        "n_bonds": float(len(priced)), "shortest": knots[0], "longest": knots[-1]})
    return out


def day_tables(day: dt.date, rows: List[dict], bonds: Dict[str, dict]) -> Tuple[List[dict], List[dict]]:
    """(sa_prices rows, sa_curves rows) for one auction date. No usable bond, no curve."""
    priced = price_rows(rows, bonds)
    if not priced:
        return [], []
    curve = build(priced)
    for r in priced:
        r.pop("_bond")
    return priced, curve_rows(day, priced, curve)


def crosscheck(curves: pd.DataFrame, reference: pd.DataFrame) -> pd.DataFrame:
    """One row per month with an auction: the 10-year par and zero averaged inside the knots, the
    two series, and the par yield less each in basis points."""
    at = curves[curves["tenor"] == CHECK_TENOR].copy()
    at["month"] = at["auction_date"].map(lambda d: dt.date(d.year, d.month, 1))
    inside = at[~at["extrapolated"].astype(bool)]
    g = at.groupby("month")
    out = pd.DataFrame({"auction_dates": g.size().astype(float)})
    gi = inside.groupby("month")
    out["inside_knots"] = gi.size().reindex(out.index).fillna(0).astype(float)
    out["par_10y"] = gi["par_yield"].mean().reindex(out.index)
    out["zero_10y"] = gi["zero_rate"].mean().reindex(out.index)
    for series in ("oecd_irlt", "imf_ifs"):
        ref = reference[reference["series"] == series].set_index("date")["percent"] if len(reference) else pd.Series(dtype=float)
        out[series] = ref.reindex(out.index)
    out["par_minus_oecd_bp"] = (out["par_10y"] - out["oecd_irlt"]) * 100.0
    out["par_minus_imf_bp"] = (out["par_10y"] - out["imf_ifs"]) * 100.0
    return out.reset_index().rename(columns={"index": "month"})


def summary(check: pd.DataFrame) -> Dict[str, Optional[float]]:
    """Mean and mean absolute gap to each series over the months both sides have."""
    out: Dict[str, Optional[float]] = {}
    for series in ("oecd", "imf"):
        gap = check[f"par_minus_{series}_bp"].dropna()
        out[f"{series}_months"] = float(len(gap))
        out[f"{series}_mean_bp"] = float(gap.mean()) if len(gap) else None
        out[f"{series}_mae_bp"] = float(gap.abs().mean()) if len(gap) else None
    return out
