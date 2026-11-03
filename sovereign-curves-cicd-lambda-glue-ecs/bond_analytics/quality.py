"""Quote checks the loader runs on each day's prices. They flag a row; they never change it.

``off_neighbours``: a note or bond whose every remaining payment falls inside the day's bill
maturities is worth its payments discounted on the bills, so its dirty price can be checked
without a fitted curve. The gap to that value is not zero in general: notes trade cheap to
bills, by two or three 32nds six to ten months out in October 2026 and by more in March 2020.
So each gap is compared with the median gap of the other notes and bonds maturing within 31
days of it, and a quote more than four 32nds from its neighbours is flagged. FedInvest quotes
notes and bonds in 32nds, so a 32nd either way is the quote's own rounding.

On 2026-10-23 the 6.5% bond maturing 2026-11-15 was quoted 99-29 (99.90625) clean, 7.2 32nds
below its value on the bills; the 4.625% and 2% notes maturing the same day were 1.0 and 0.5
32nds below. The bond is the quote, not the market.

Standard library only: the Glue Python Shell loader runs it on Python 3.9.
"""

from __future__ import annotations

import bisect
import datetime as dt
import math
from typing import Dict, List, Optional, Sequence, Tuple

from . import dates

TICK = 1.0 / 32.0
OFF_NEIGHBOURS_TICKS = 4.0
NEIGHBOUR_DAYS = 31
MIN_NEIGHBOURS = 2


class BillCurve:
    """Discount factors from the day's bills, log-linear in time between bill maturities."""

    def __init__(self, points: Sequence[Tuple[float, float]]):
        pts = sorted({t: p for t, p in points if t > 0 and p > 0}.items())
        self.times = [t for t, _ in pts]
        self.logs = [math.log(p / 100.0) for _, p in pts]

    def __len__(self) -> int:
        return len(self.times)

    def covers(self, t: float) -> bool:
        return bool(self.times) and self.times[0] <= t <= self.times[-1]

    def discount(self, t: float) -> Optional[float]:
        if not self.covers(t):
            return None
        i = bisect.bisect_left(self.times, t)
        if self.times[i] == t:
            return math.exp(self.logs[i])
        t0, t1 = self.times[i - 1], self.times[i]
        a0, a1 = self.logs[i - 1], self.logs[i]
        return math.exp(a0 + (a1 - a0) * (t - t0) / (t1 - t0))


def bill_curve(securities: Sequence[dates.Security], prices: Sequence[float], settle: dt.date) -> BillCurve:
    return BillCurve(
        (dates.year_fraction(settle, s.maturity), p) for s, p in zip(securities, prices) if s.kind == "bill" and s.maturity > settle
    )


def bill_curve_gap(sec: dates.Security, dirty: float, settle: dt.date, curve: BillCurve) -> Optional[float]:
    """Quoted dirty price less its payments discounted on the bills, or None when out of range."""
    if sec.kind not in ("note", "bond"):
        return None
    flows = dates.cash_flows(sec, settle)
    value = 0.0
    for d, amount in flows:
        df = curve.discount(dates.year_fraction(settle, d))
        if df is None:
            return None
        value += amount * df
    return dirty - value


def flag_quotes(rows: List[Dict[str, object]], settle: dt.date) -> None:
    """Add ``bill_curve_gap``, ``neighbour_gap_ticks`` and ``flags`` to each row in place.

    Each row needs ``security`` (a ``dates.Security``), ``end_of_day`` and ``dirty``.
    """
    curve = bill_curve([r["security"] for r in rows], [r["end_of_day"] for r in rows], settle)
    checked = []
    for r in rows:
        r["flags"] = list(r.get("flags") or [])
        r["bill_curve_gap"] = bill_curve_gap(r["security"], r["dirty"], settle, curve)
        r["neighbour_gap_ticks"] = None
        if r["bill_curve_gap"] is not None:
            checked.append(r)
    for r in checked:
        mat = r["security"].maturity
        peers = sorted(
            q["bill_curve_gap"] for q in checked if q is not r and abs((q["security"].maturity - mat).days) <= NEIGHBOUR_DAYS
        )
        if len(peers) < MIN_NEIGHBOURS:
            continue
        mid = len(peers) // 2
        median = peers[mid] if len(peers) % 2 else (peers[mid - 1] + peers[mid]) / 2.0
        r["neighbour_gap_ticks"] = (r["bill_curve_gap"] - median) / TICK
        if abs(r["neighbour_gap_ticks"]) > OFF_NEIGHBOURS_TICKS:
            r["flags"].append("off_neighbours")
