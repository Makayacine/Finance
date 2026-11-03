"""Security static from the Treasury's auction records, and on-the-run ranks.

FedInvest gives a CUSIP, a type, a coupon and a maturity, but no issue date, so it cannot say
which note is the on-the-run 10-year or whether a first coupon is odd. The Treasury publishes
every auction since 1979 through its Fiscal Data API ("Treasury Securities Auctions Data"),
one record per auction, reopenings included. One record per CUSIP is kept: its original
auction, which carries the original term, the dated date and the first coupon's date and
length. Standard library only.
"""

from __future__ import annotations

import datetime as dt
from typing import Dict, Iterable, List, Optional

AUCTION_FIELDS = [
    "cusip",
    "security_type",
    "original_security_term",
    "auction_date",
    "issue_date",
    "dated_date",
    "maturity_date",
    "int_rate",
    "first_int_period",
    "first_int_payment_date",
    "reopening",
    "inflation_index_security",
    "floating_rate",
]

# The tenors the Treasury auctions on a regular calendar: the ones that have an on-the-run issue.
NOMINAL_TERMS = ("2-Year", "3-Year", "5-Year", "7-Year", "10-Year", "20-Year", "30-Year")


def _date(text: Optional[str]) -> Optional[dt.date]:
    if text in (None, "", "null"):
        return None
    return dt.date.fromisoformat(text[:10])


def _text(value: Optional[str]) -> Optional[str]:
    return None if value in (None, "", "null") else value


def static_by_cusip(auctions: Iterable[Dict[str, str]]) -> Dict[str, Dict[str, object]]:
    """One record per CUSIP, from its original auction (the earliest when none is marked)."""
    best: Dict[str, Dict[str, str]] = {}
    for rec in auctions:
        cusip = rec["cusip"]
        cur = best.get(cusip)
        rank = (rec.get("reopening") != "No", rec.get("issue_date") or "9999")
        if cur is None or rank < (cur.get("reopening") != "No", cur.get("issue_date") or "9999"):
            best[cusip] = rec
    out = {}
    for cusip, rec in best.items():
        out[cusip] = {
            "cusip": cusip,
            "auction_type": _text(rec.get("security_type")),
            "original_term": _text(rec.get("original_security_term")),
            "issue_date": _date(rec.get("issue_date")),
            "dated_date": _date(rec.get("dated_date")),
            "first_coupon_date": _date(rec.get("first_int_payment_date")),
            "first_period": _text(rec.get("first_int_period")),
            "inflation_indexed": rec.get("inflation_index_security") == "Yes",
            "floating": rec.get("floating_rate") == "Yes",
        }
    return out


def run_ranks(static: Dict[str, Dict[str, object]], as_of: dt.date) -> Dict[str, int]:
    """How many newer issues of the same nominal tenor were outstanding on ``as_of``.

    0 is the on-the-run issue, 1 the first off-the-run, and so on. Only fixed-coupon notes and
    bonds of the regular tenors are ranked; any other CUSIP is absent from the result.
    """
    by_term: Dict[str, List[tuple]] = {}
    for cusip, rec in static.items():
        if rec["auction_type"] not in ("Note", "Bond") or rec["inflation_indexed"] or rec["floating"]:
            continue
        term = rec["original_term"]
        issued = rec["issue_date"]
        if term not in NOMINAL_TERMS or issued is None or issued > as_of:
            continue
        by_term.setdefault(term, []).append((issued, cusip))
    ranks = {}
    for issues in by_term.values():
        for i, (_, cusip) in enumerate(sorted(issues, reverse=True)):
            ranks[cusip] = i
    return ranks


def run_label(rank: Optional[int]) -> str:
    if rank == 0:
        return "on-the-run"
    if rank == 1:
        return "first off-the-run"
    return "off-the-run"
