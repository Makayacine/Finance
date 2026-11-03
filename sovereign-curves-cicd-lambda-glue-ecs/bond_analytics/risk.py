"""Interest-rate risk per security, through FIA's own functions.

- **DV01, modified duration and convexity** are taken against the bond's own semiannual yield:
  FIA's ``YTM`` gives the yield from the dirty price, ``create_flat_curve`` turns it into a
  flat curve, and ``dollar``, ``duration`` and ``convexity`` with ``compounding=2`` are the
  derivatives with respect to that yield. DV01 is the dollar duration over 10,000: the price
  change per 100 face for one basis point.
- **Key-rate DV01s at 2, 5, 10, 20 and 30 years** come from the day's fitted curve, shifted by
  the bond's own z-spread so that it reprices the bond exactly, then bumped by one basis point
  in the shape of a triangle that peaks at one key tenor and reaches zero at its neighbours;
  the first bump is flat to the short end and the last flat to the long end. The five
  triangles add up to one everywhere, so the five DV01s add up to the curve DV01 of a parallel
  basis-point shift, which is reported beside them as the check.

FIA's own risk is parallel shifts and per-knot bumps of a piecewise linear curve; the triangles
here are what a per-knot bump does on a curve whose knots are the key tenors.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, Sequence

from . import FIA
from .svensson import SvenssonCurve

KEY_TENORS: Sequence[float] = (2.0, 5.0, 10.0, 20.0, 30.0)
BP = 0.0001


def key_rate_weight(t: float, k: int, tenors: Sequence[float] = KEY_TENORS) -> float:
    """The k-th triangle at time ``t``: 1 at ``tenors[k]``, 0 at its neighbours and beyond."""
    tk = tenors[k]
    if t <= tk:
        if k == 0:
            return 1.0
        lo = tenors[k - 1]
        return 0.0 if t <= lo else (t - lo) / (tk - lo)
    if k == len(tenors) - 1:
        return 1.0
    hi = tenors[k + 1]
    return 0.0 if t >= hi else (hi - t) / (hi - tk)


def _bump(k: int, size: float) -> Callable[[float], float]:
    return lambda t: size * key_rate_weight(t, k)


def z_spread(bond: "FIA.relative_bond", curve: SvenssonCurve, tol: float = 1e-10) -> float:
    """The flat continuously compounded spread that makes ``curve`` reprice ``bond``."""
    target = bond.get_price()
    s = 0.0
    for _ in range(50):
        shifted = SvenssonCurve(curve.params, s)
        pv = bond.price(shifted)
        slope = -sum(t * shifted.discount_factor(t) * c for t, c in zip(bond.get_dates(), bond.get_cashflows()))
        step = (pv - target) / slope
        s -= step
        if abs(step) < tol:
            break
    return s


@dataclass
class Risk:
    ytm: float  # percent, semiannual
    dv01: float  # per 100 face
    modified_duration: float
    convexity: float
    z_spread_bp: float
    curve_dv01: float
    key_rate_dv01: Dict[float, float]


def measure(bond: "FIA.relative_bond", curve_params: Sequence[float]) -> Risk:
    price = bond.get_price()
    ytm = bond.YTM(price, compounding=2)
    flat = FIA.create_flat_curve(ytm, compounding=2)
    dv01 = bond.dollar(flat, compounding=2) * BP
    mod = bond.duration(flat, compounding=2)
    conv = bond.convexity(flat, compounding=2)

    base = SvenssonCurve(curve_params)
    s = z_spread(bond, base)
    fitted = SvenssonCurve(curve_params, s)
    curve_dv01 = (bond.price(fitted.shift(-0.01)) - bond.price(fitted.shift(0.01))) / 2.0
    krd = {}
    for k, tenor in enumerate(KEY_TENORS):
        down = bond.price(fitted.with_bump(_bump(k, -BP)))
        up = bond.price(fitted.with_bump(_bump(k, BP)))
        krd[tenor] = (down - up) / 2.0
    return Risk(ytm, dv01, mod, conv, s / BP, curve_dv01, krd)
