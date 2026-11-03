"""A day's FedInvest rows as priced securities: settlement, accrued, dirty price, FIA bond."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional

from . import dates
from .fedinvest import parse_us_date, security_from_row
from .securities import run_label, run_ranks


@dataclass
class Quote:
    security: dates.Security
    price_date: dt.date
    settle: dt.date
    clean: float
    accrued: float
    dirty: float
    callable: bool
    run_rank: Optional[int]
    years: float
    bond: object  # FIA.relative_bond

    @property
    def run(self) -> str:
        return run_label(self.run_rank)


def build_quotes(
    rows: Iterable[Dict[str, str]],
    static: Dict[str, Dict[str, object]],
    price_date: dt.date,
    kinds: Iterable[str] = dates.CURVE_KINDS,
) -> List[Quote]:
    """Every row of the wanted kinds with a positive end-of-day price that has not matured."""
    settle = dates.settlement_date(price_date)
    ranks = run_ranks(static, settle)
    kinds = set(kinds)
    out = []
    for row in rows:
        sec = security_from_row(row, static.get(row["cusip"]))
        if sec.kind not in kinds or sec.maturity <= settle:
            continue
        clean = float(row["end_of_day"])
        if clean <= 0:
            continue
        bond = dates.to_relative_bond(sec, settle, clean)
        accrued = dates.accrued_interest(sec, settle)
        out.append(
            Quote(
                security=sec,
                price_date=price_date,
                settle=settle,
                clean=clean,
                accrued=accrued,
                dirty=clean + accrued,
                callable=parse_us_date(row["call_date"]) is not None,
                run_rank=ranks.get(sec.cusip),
                years=dates.year_fraction(settle, sec.maturity),
                bond=bond,
            )
        )
    return out


def fit_sample(quotes: List[Quote], min_years: float = 0.25) -> List[Quote]:
    """The notes and bonds a GSW-style Svensson fit uses.

    As GSW: no bills, no callable bonds, nothing within three months of maturity, no on-the-run
    or first off-the-run issue, and no bond first issued as a 20-year.
    """
    return [
        q
        for q in quotes
        if q.security.kind in ("note", "bond")
        and not q.callable
        and q.years >= min_years
        and (q.run_rank is None or q.run_rank >= 2)
        and q.security.original_term != "20-Year"
    ]


def bootstrap_candidates(quotes: List[Quote]):
    from .bootstrap import Candidate

    return [Candidate(q.security.cusip, q.security.kind, q.years, q.run_rank, q.callable, q.bond) for q in quotes]
