"""The Svensson curve: its formula, a fit to bond prices, and an FIA curve that uses it.

FIA's own Nelson-Siegel builder is broken (``exp`` is undefined in it and its result is
discarded), and every FIA curve is a set of knots joined by an interpolator. ``SvenssonCurve``
subclasses FIA's ``curve`` and overrides only ``discount_factor``, which is the one method FIA's
pricing and risk functions call, so ``relative_bond.price``, ``duration``, ``convexity`` and
the rest run on it unchanged.

Zero rates are continuously compounded, in percent, as in the GSW file:

    y(n) = b0 + b1 L1(n) + b2 (L1(n) - e^(-n/t1)) + b3 (L2(n) - e^(-n/t2)),  Lk(n) = (1 - e^(-n/tk)) / (n/tk)
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Sequence

import numpy as np

from . import FIA


def _loading(n, tau):
    x = np.asarray(n, dtype=float) / tau
    small = np.abs(x) < 1e-8
    safe = np.where(small, 1.0, x)
    level = np.where(small, 1.0 - x / 2.0, -np.expm1(-safe) / safe)
    return level, np.exp(-x)


def zero_rate(n, params) -> np.ndarray:
    """Continuously compounded zero rate in percent at maturity ``n`` years."""
    b0, b1, b2, b3, t1, t2 = params
    l1, e1 = _loading(n, t1)
    y = b0 + b1 * l1 + b2 * (l1 - e1)
    if b3 and t2 is not None and not (isinstance(t2, float) and math.isnan(t2)):
        l2, e2 = _loading(n, t2)
        y = y + b3 * (l2 - e2)
    return y


def zero_rate_scalar(n: float, params) -> float:
    """``zero_rate`` for one maturity, with ``math`` rather than numpy: FIA calls it per cash flow."""
    b0, b1, b2, b3, t1, t2 = params
    x = n / t1
    e1 = math.exp(-x)
    l1 = 1.0 - x / 2.0 if abs(x) < 1e-8 else (1.0 - e1) / x
    y = b0 + b1 * l1 + b2 * (l1 - e1)
    if b3 and t2 is not None and t2 == t2:
        x2 = n / t2
        e2 = math.exp(-x2)
        l2 = 1.0 - x2 / 2.0 if abs(x2) < 1e-8 else (1.0 - e2) / x2
        y += b3 * (l2 - e2)
    return y


def discount(n, params) -> np.ndarray:
    n = np.asarray(n, dtype=float)
    return np.exp(-zero_rate(n, params) * n / 100.0)


def par_yield(n: float, params) -> float:
    """Semiannual par yield in percent for a bond of ``n`` years, as GSW's SVENPY series."""
    times = np.arange(n, 0, -0.5)[::-1]
    df = discount(times, params)
    return 200.0 * (1.0 - df[-1]) / df.sum()


class SvenssonCurve(FIA.curve):
    """An FIA curve whose discount factors come from Svensson parameters, plus a flat spread.

    ``spread`` is continuously compounded, in decimal. ``shift`` moves it, so a parallel shift
    keeps the curve's shape, where FIA's ``shift`` would rebuild a curve as piecewise linear.
    """

    def __init__(self, params: Sequence[float], spread: float = 0.0, bump=None):
        super().__init__()
        self.params = tuple(params)
        self.spread = spread
        self.bump_fn = bump

    def zero(self, time: float) -> float:
        rate = zero_rate_scalar(time, self.params) / 100.0 + self.spread
        if self.bump_fn is not None:
            rate += self.bump_fn(time)
        return rate

    def discount_factor(self, time):
        return math.exp(-self.zero(time) * time)

    def get_yield(self, time):
        return 100.0 * self.zero(time)

    def shift(self, delta):
        return SvenssonCurve(self.params, self.spread + delta / 100.0, self.bump_fn)

    def with_bump(self, bump) -> "SvenssonCurve":
        return SvenssonCurve(self.params, self.spread, bump)

    def copy(self):
        return SvenssonCurve(self.params, self.spread, self.bump_fn)


# --- fitting ----------------------------------------------------------------------------------


@dataclass
class CashFlowMatrix:
    """Bonds as a padded matrix: one row per bond, zero amounts beyond its last payment."""

    times: np.ndarray
    amounts: np.ndarray
    prices: np.ndarray

    @classmethod
    def from_bonds(cls, bonds: List["FIA.relative_bond"]) -> "CashFlowMatrix":
        width = max(len(b.get_dates()) for b in bonds)
        times = np.ones((len(bonds), width))
        amounts = np.zeros((len(bonds), width))
        for i, b in enumerate(bonds):
            k = len(b.get_dates())
            times[i, :k] = b.get_dates()
            amounts[i, :k] = b.get_cashflows()
        prices = np.array([b.get_price() for b in bonds], dtype=float)
        return cls(times, amounts, prices)

    def price(self, params) -> np.ndarray:
        return (self.amounts * discount(self.times, params)).sum(axis=1)

    def modified_duration(self, params) -> np.ndarray:
        df = discount(self.times, params)
        pv = self.amounts * df
        return (pv * self.times).sum(axis=1) / pv.sum(axis=1)


PARAM_NAMES = ("beta0", "beta1", "beta2", "beta3", "tau1", "tau2")


@dataclass
class SvenssonFit:
    params: tuple
    rmse_bp: float  # duration-weighted price errors, in basis points of yield
    n_bonds: int
    starts: int
    cost: float
    at_bound: tuple = ()  # the parameters that ended on a bound of the search


LOWER = np.array([0.0, -30.0, -60.0, -60.0, 0.05, 0.05])
UPPER = np.array([30.0, 30.0, 60.0, 60.0, 30.0, 50.0])
TAU_STARTS = [(0.7, 3.0), (1.5, 8.0), (3.0, 12.0), (0.5, 15.0), (2.0, 20.0)]


def fit(bonds: List["FIA.relative_bond"], start: Optional[Sequence[float]] = None) -> SvenssonFit:
    """Fit Svensson parameters to dirty prices, as GSW do.

    The objective is the sum of squared price errors, each divided by the bond's duration, which
    makes each term about the bond's yield error. Several starting points for the two decay
    parameters are tried, and ``start`` (yesterday's parameters, say) as well when given.
    """
    from scipy.optimize import least_squares

    cf = CashFlowMatrix.from_bonds(bonds)
    long_end = cf.times.max(axis=1)
    order = np.argsort(long_end)
    approx = -100.0 * np.log(cf.prices / cf.amounts.sum(axis=1)) / long_end
    b0 = float(np.clip(np.median(approx[order][-max(3, len(order) // 5):]), 0.01, 25))
    b1 = float(np.clip(np.median(approx[order][: max(3, len(order) // 5)]) - b0, -25, 25))

    starts = [np.array([b0, b1, 0.0, 0.0, t1, t2]) for t1, t2 in TAU_STARTS]
    if start is not None:
        starts.insert(0, np.clip(np.array(start, dtype=float), LOWER + 1e-9, UPPER - 1e-9))

    def residuals(theta):
        model = cf.price(theta)
        weights = cf.modified_duration(theta)
        return (model - cf.prices) / weights

    best = None
    for x0 in starts:
        res = least_squares(residuals, x0, bounds=(LOWER, UPPER), method="trf", x_scale="jac", max_nfev=400)
        if best is None or res.cost < best.cost:
            best = res
    resid = residuals(best.x)
    rmse_bp = float(np.sqrt(np.mean(resid**2)) * 100.0)
    return SvenssonFit(
        tuple(float(v) for v in best.x), rmse_bp, len(bonds), len(starts), float(best.cost), params_at_bound(best.x)
    )


def params_at_bound(x, tol: float = 1e-6) -> tuple:
    """Names of the parameters within ``tol`` of the search bounds: a constrained fit, said aloud."""
    x = np.asarray(x, dtype=float)
    hit = (np.abs(x - LOWER) <= tol * np.maximum(1.0, np.abs(LOWER))) | (np.abs(x - UPPER) <= tol * np.maximum(1.0, np.abs(UPPER)))
    return tuple(name for name, h in zip(PARAM_NAMES, hit) if h)
