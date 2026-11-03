"""Calendar dates in, FIA's float years out.

FIA measures time as float years from "today" and has no calendar, no day count and no accrued
interest. This module supplies all three for US Treasuries, so that FIA's own bootstrap, pricing
and risk functions run unchanged:

- settlement is the next business day after the price date (T+1);
- coupons fall every six months, counted back from maturity, with the end-of-month rule;
- accrued interest is actual/actual (ICMA), including odd first coupons;
- a cash flow's time is its actual days from settlement over 365.25.

Standard library only: the Glue Python Shell loader imports it on Python 3.9.
"""

from __future__ import annotations

import calendar
import datetime as dt
from dataclasses import dataclass
from functools import lru_cache
from typing import List, Optional, Tuple

YEAR_DAYS = 365.25
FACE = 100.0

CURVE_KINDS = ("bill", "note", "bond")


@dataclass(frozen=True)
class Security:
    """One Treasury security, with what FedInvest gives and what the auction record adds."""

    cusip: str
    kind: str  # bill, note, bond, tips, frn
    coupon: float  # percent a year
    maturity: dt.date
    dated_date: Optional[dt.date] = None
    first_coupon_date: Optional[dt.date] = None
    first_period: Optional[str] = None  # Normal, Short or Long, from the auction record
    issue_date: Optional[dt.date] = None
    original_term: Optional[str] = None


# --- business days ----------------------------------------------------------------------------


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> dt.date:
    first = dt.date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + dt.timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> dt.date:
    last = dt.date(year, month, calendar.monthrange(year, month)[1])
    return last - dt.timedelta(days=(last.weekday() - weekday) % 7)


@lru_cache(maxsize=None)
def fed_holidays(year: int) -> frozenset:
    """The Federal Reserve's holidays, on the days Fedwire is closed for them.

    A fixed-date holiday on a Sunday is observed the Monday after; one on a Saturday is not
    moved, because the Reserve Banks open the Friday before.
    """
    fixed = [dt.date(year, 1, 1), dt.date(year, 7, 4), dt.date(year, 11, 11), dt.date(year, 12, 25)]
    if year >= 2022:
        fixed.append(dt.date(year, 6, 19))
    days = set()
    for d in fixed:
        if d.weekday() == 6:
            days.add(d + dt.timedelta(days=1))
        elif d.weekday() < 5:
            days.add(d)
    days.update(
        {
            _nth_weekday(year, 1, 0, 3),  # Martin Luther King Jr.
            _nth_weekday(year, 2, 0, 3),  # Washington's Birthday
            _last_weekday(year, 5, 0),  # Memorial Day
            _nth_weekday(year, 9, 0, 1),  # Labor Day
            _nth_weekday(year, 10, 0, 2),  # Columbus Day
            _nth_weekday(year, 11, 3, 4),  # Thanksgiving
        }
    )
    return frozenset(days)


def is_business_day(d: dt.date) -> bool:
    return d.weekday() < 5 and d not in fed_holidays(d.year)


def add_business_days(d: dt.date, n: int) -> dt.date:
    step = 1 if n >= 0 else -1
    remaining = abs(n)
    while remaining:
        d += dt.timedelta(days=step)
        if is_business_day(d):
            remaining -= 1
    return d


def settlement_date(price_date: dt.date) -> dt.date:
    """FedInvest's end-of-day price is for settlement the next business day.

    Decided from the bills: on 2026-10-30 the bills maturing 3, 5 and 10 November price at
    discount yields of 3.82%, 3.88% and 3.88% counted from Monday 2 November, and of 0.95%,
    1.94% and 2.82% counted from the price date itself.
    """
    return add_business_days(price_date, 1)


# --- coupon schedule --------------------------------------------------------------------------


def is_month_end(d: dt.date) -> bool:
    return d.day == calendar.monthrange(d.year, d.month)[1]


def add_months(d: dt.date, months: int, month_end: bool) -> dt.date:
    """Move by whole months; with ``month_end`` the result is the last day of its month."""
    y, m = divmod(d.month - 1 + months, 12)
    y += d.year
    m += 1
    last = calendar.monthrange(y, m)[1]
    return dt.date(y, m, last if month_end else min(d.day, last))


def regular_coupon(maturity: dt.date, k: int) -> dt.date:
    """The k-th regular coupon date counted back from maturity (k = 0 is maturity)."""
    return add_months(maturity, -6 * k, is_month_end(maturity))


def coupon_period(sec: Security, settle: dt.date) -> Tuple[dt.date, dt.date]:
    """The regular coupon dates either side of ``settle``: previous <= settle < next."""
    k = 0
    nxt = sec.maturity
    while True:
        prev = regular_coupon(sec.maturity, k + 1)
        if prev <= settle:
            return prev, nxt
        k += 1
        nxt = prev


def _odd_first(sec: Security, settle: dt.date) -> bool:
    return (
        sec.first_period in ("Short", "Long")
        and sec.dated_date is not None
        and sec.first_coupon_date is not None
        and settle < sec.first_coupon_date
    )


def coupon_dates_after(sec: Security, settle: dt.date) -> List[dt.date]:
    """Every payment date strictly after settlement, oldest first.

    A coupon falling on the settlement date belongs to the seller, so it is not included.
    """
    if sec.kind == "bill":
        return [sec.maturity] if sec.maturity > settle else []
    dates = []
    k = 0
    while True:
        d = regular_coupon(sec.maturity, k)
        if d <= settle:
            break
        if _odd_first(sec, settle) and d < sec.first_coupon_date:
            break
        dates.append(d)
        k += 1
    return dates[::-1]


def _odd_first_amounts(sec: Security, settle: dt.date) -> Tuple[float, float]:
    """(first coupon, accrued interest) per 100 for a settlement inside an odd first period.

    The Treasury's rule: a short first coupon is the half-coupon scaled by the days from the
    dated date over the days in the regular period that ends on the first coupon date; a long
    one adds the same fraction for the stub before the first regular period starts.
    """
    half = sec.coupon / 2.0
    first = sec.first_coupon_date
    reg_start = add_months(first, -6, is_month_end(sec.maturity))
    if sec.first_period == "Short":
        period = (first - reg_start).days
        payment = half * (first - sec.dated_date).days / period
        accrued = half * max((settle - sec.dated_date).days, 0) / period
        return payment, accrued
    stub_start = add_months(reg_start, -6, is_month_end(sec.maturity))
    stub_days = (reg_start - stub_start).days
    stub_frac = (reg_start - sec.dated_date).days / stub_days
    payment = half * (1.0 + stub_frac)
    if settle < reg_start:
        accrued = half * max((settle - sec.dated_date).days, 0) / stub_days
    else:
        accrued = half * stub_frac + half * (settle - reg_start).days / (first - reg_start).days
    return payment, accrued


def accrued_interest(sec: Security, settle: dt.date) -> float:
    """Actual/actual (ICMA) accrued interest per 100 face at ``settle``. Zero for a bill."""
    if sec.kind == "bill" or sec.coupon == 0.0 or settle >= sec.maturity:
        return 0.0
    if _odd_first(sec, settle):
        return _odd_first_amounts(sec, settle)[1]
    if sec.dated_date is not None and settle <= sec.dated_date:
        return 0.0
    prev, nxt = coupon_period(sec, settle)
    return (sec.coupon / 2.0) * (settle - prev).days / (nxt - prev).days


def cash_flows(sec: Security, settle: dt.date) -> List[Tuple[dt.date, float]]:
    """(date, amount per 100 face) for every payment strictly after settlement."""
    dates = coupon_dates_after(sec, settle)
    if sec.kind == "bill":
        return [(d, FACE) for d in dates]
    half = sec.coupon / 2.0
    flows = [(d, half) for d in dates]
    if flows and _odd_first(sec, settle):
        flows[0] = (flows[0][0], _odd_first_amounts(sec, settle)[0])
    if flows:
        flows[-1] = (flows[-1][0], flows[-1][1] + FACE)
    return flows


def year_fraction(settle: dt.date, d: dt.date) -> float:
    """FIA's time axis: actual days from settlement over 365.25."""
    return (d - settle).days / YEAR_DAYS


def dirty_price(sec: Security, settle: dt.date, clean: float) -> float:
    return clean + accrued_interest(sec, settle)


def to_relative_bond(sec: Security, settle: dt.date, clean: Optional[float] = None):
    """An FIA ``relative_bond``: times in years from settlement, payments per 100, dirty price."""
    from . import FIA

    flows = cash_flows(sec, settle)
    bond = FIA.relative_bond([float(a) for _, a in flows], [year_fraction(settle, d) for d, _ in flows])
    if clean is not None:
        bond.set_price(float(dirty_price(sec, settle, clean)))
    return bond
