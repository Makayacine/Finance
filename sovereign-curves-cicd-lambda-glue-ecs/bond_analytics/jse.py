"""South African government bonds: the JSE's yield-to-price formula, and FIA bonds from it.

South African bonds trade on yield and settle on price. The JSE's Bond Pricing Formula turns one
into the other:

- coupons are semiannual, on two fixed day-and-month dates (28/02 and 31/08 for R2048, leap
  year or not), and the last one falls on the redemption date;
- the books close ten days before each coupon date, as each bond's terms of issue set out ("the
  Register will be closed from 21st to 30th March" for a 31 March coupon). A trade settling on
  or after the books-closed date is ex-coupon: the buyer does not get the next coupon, and the
  accrued interest is negative, the days from settlement to that coupon date;
- accrued interest is actual/365, coupon times days over 365;
- the all-in price discounts at ``f = 1 / (1 + y/200)``: the next coupon when cum, then ``N``
  more half-years of coupons and the redemption, all brought back over the broken period
  ``(NCD - settlement) / (NCD - LCD)`` as ``f`` to that power;
- the all-in price and the accrued interest are rounded to five decimals, and the clean price is
  the difference.

Inside a bond's last coupon period the formula changes to simple interest. No auction since 2010
settled there (the shortest bond auctioned had 2.82 years left), so ``price`` refuses that case
rather than carry a branch no auction reaches.

The check is the Treasury's own: its debt schedule gives each bond's initial issue price, and the
first auction's clearing yield, priced here for the issue's settlement date, reproduces it to
within 0.001 for 11 of the 12 bonds first sold at an auction since 2010 whose yield passes its
checks, and R2033's to 0.005 (``tests/test_sa_pricing.py`` holds eight). The Treasury publishes some issue prices
clean (R213, R2030) and some all-in (R2032, R2044), and each matches its own kind.

A three-legged bond (R186, R2044 and R2048 before they were split) is priced on its middle leg,
the convention the Treasury describes, so here it is a bullet to that date.

``to_relative_bond`` gives FIA the same cash flows, each payment's time its actual days from
settlement over 365, priced at the all-in price, so FIA's bootstrap and risk run on it unchanged.
Standard library only.
"""

from __future__ import annotations

import calendar
import datetime as dt
from dataclasses import dataclass
from typing import List, Optional, Tuple

FACE = 100.0
BOOKS_CLOSED_DAYS = 10
YEAR_DAYS = 365.0


@dataclass(frozen=True)
class Bond:
    code: str
    coupon: float  # percent a year
    maturity: dt.date  # the pricing maturity: the middle leg of a three-legged bond
    coupon_months: Tuple[int, int]
    coupon_days: Tuple[int, int]


def _on(year: int, month: int, day: int) -> dt.date:
    return dt.date(year, month, min(day, calendar.monthrange(year, month)[1]))


def coupon_dates(bond: Bond, after: dt.date) -> List[dt.date]:
    """Every coupon date strictly after ``after`` up to the maturity, oldest first."""
    out = set()
    for year in range(after.year, bond.maturity.year + 1):
        for m, d in zip(bond.coupon_months, bond.coupon_days):
            c = _on(year, m, d)
            if after < c <= bond.maturity:
                out.add(c)
    return sorted(out)


def previous_coupon(bond: Bond, ncd: dt.date) -> dt.date:
    """The coupon date before ``ncd``: six months back on the bond's own day-and-month pair."""
    cands = [_on(y, m, d) for y in (ncd.year - 1, ncd.year) for m, d in zip(bond.coupon_months, bond.coupon_days)]
    return max(c for c in cands if c < ncd)


def books_closed(coupon_date: dt.date) -> dt.date:
    return coupon_date - dt.timedelta(days=BOOKS_CLOSED_DAYS)


@dataclass
class Price:
    settle: dt.date
    ncd: dt.date
    lcd: dt.date
    cum: bool
    n: int  # whole half-years from the next coupon date to maturity
    broken_period: float
    all_in: float
    accrued: float
    clean: float


def price(bond: Bond, settle: dt.date, yield_pct: float, rounded: bool = True) -> Price:
    """The JSE all-in price, accrued interest and clean price per R100 nominal.

    ``rounded=False`` keeps every decimal, for the derivatives in ``dv01``.
    """
    dates = coupon_dates(bond, settle)
    if len(dates) < 2:
        raise ValueError(f"{bond.code}: {settle} is in the last coupon period, where the formula is simple interest")
    ncd, n = dates[0], len(dates) - 1
    lcd = previous_coupon(bond, ncd)
    cum = settle < books_closed(ncd)
    half = bond.coupon / 2.0
    f = 1.0 / (1.0 + yield_pct / 200.0)
    bp = (ncd - settle).days / (ncd - lcd).days
    annuity = sum(f**k for k in range(1, n + 1))
    all_in = f**bp * ((half if cum else 0.0) + half * annuity + FACE * f**n)
    days = (settle - lcd).days if cum else (settle - ncd).days
    accrued = days * bond.coupon / YEAR_DAYS
    if rounded:
        all_in, accrued = round(all_in, 5), round(accrued, 5)
    clean = round(all_in - accrued, 5) if rounded else all_in - accrued
    return Price(settle, ncd, lcd, cum, n, bp, all_in, accrued, clean)


def dv01(bond: Bond, settle: dt.date, yield_pct: float) -> float:
    """The all-in price change per R100 for one basis point of the JSE yield, centred."""
    down = price(bond, settle, yield_pct - 0.01, rounded=False).all_in
    up = price(bond, settle, yield_pct + 0.01, rounded=False).all_in
    return (down - up) / 2.0


def cash_flows(bond: Bond, settle: dt.date) -> List[Tuple[dt.date, float]]:
    """(date, amount per R100) the buyer settling on ``settle`` receives: no next coupon when ex."""
    dates = coupon_dates(bond, settle)
    half = bond.coupon / 2.0
    flows = [(d, half) for d in dates]
    if dates and settle >= books_closed(dates[0]):
        flows = flows[1:]
    flows[-1] = (flows[-1][0], flows[-1][1] + FACE)
    return flows


def year_fraction(settle: dt.date, d: dt.date) -> float:
    return (d - settle).days / YEAR_DAYS


def to_relative_bond(bond: Bond, settle: dt.date, all_in: Optional[float] = None):
    """An FIA ``relative_bond`` with the JSE cash flows; with ``all_in``, priced at it."""
    from . import FIA

    flows = cash_flows(bond, settle)
    rb = FIA.relative_bond([float(a) for _, a in flows], [year_fraction(settle, d) for d, _ in flows])
    if all_in is not None:
        rb.set_price(float(all_in))
    return rb


def bond_from_static(rec: dict) -> Bond:
    """A ``Bond`` from one ``sa_bonds`` row."""
    months = tuple(int(x) for x in str(rec["coupon_months"]).split(","))
    days = tuple(int(x) for x in str(rec["coupon_days"]).split(","))
    maturity = rec["pricing_maturity"]
    if not isinstance(maturity, dt.date):
        maturity = dt.date.fromisoformat(str(maturity)[:10])
    return Bond(rec["bond_code"], float(rec["coupon"]), maturity, months, days)
