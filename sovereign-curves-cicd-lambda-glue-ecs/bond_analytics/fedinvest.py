"""Parse the FedInvest price page, and map its rows to ``Security`` records.

The page is HTML with one ``table.data1`` of eight columns. A day with no prices, such as a
federal holiday, comes back as the same page without that table. Standard library only, so the
Glue Python Shell loader can use it as it is.
"""

from __future__ import annotations

import datetime as dt
import re
from html.parser import HTMLParser
from typing import Dict, List, Optional

from .dates import Security

COLUMNS = ["cusip", "security_type", "rate", "maturity_date", "call_date", "buy", "sell", "end_of_day"]
HEADER = ["CUSIP", "SECURITY TYPE", "RATE", "MATURITY DATE", "CALL DATE", "BUY", "SELL", "END OF DAY"]

KINDS = {
    "MARKET BASED BILL": "bill",
    "MARKET BASED NOTE": "note",
    "MARKET BASED BOND": "bond",
    "TIPS": "tips",
    "MARKET BASED FRN": "frn",
}

_PRICES_FOR = re.compile(r"Prices For:\s*([A-Za-z]+ \d{1,2}, \d{4})")


class FedInvestFormatError(ValueError):
    """The page does not have the shape this parser was written against."""


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_table = False
        self.depth = 0
        self.row: Optional[List[str]] = None
        self.cell: Optional[List[str]] = None
        self.rows: List[List[str]] = []
        self.found = False

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            if self.in_table:
                self.depth += 1
            elif ("class", "data1") in attrs:
                self.in_table = True
                self.found = True
        elif self.in_table and tag == "tr":
            self.row = []
        elif self.in_table and tag in ("td", "th") and self.row is not None:
            self.cell = []

    def handle_endtag(self, tag):
        if tag == "table" and self.in_table:
            if self.depth:
                self.depth -= 1
            else:
                self.in_table = False
        elif self.in_table and tag in ("td", "th") and self.cell is not None and self.row is not None:
            self.row.append(" ".join("".join(self.cell).split()))
            self.cell = None
        elif self.in_table and tag == "tr" and self.row is not None:
            if self.row:
                self.rows.append(self.row)
            self.row = None

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)


def page_date(html: str) -> Optional[dt.date]:
    """The date the page says its prices are for, or None if it does not say."""
    m = _PRICES_FOR.search(html)
    return dt.datetime.strptime(m.group(1), "%B %d, %Y").date() if m else None


def parse_prices(html: str, price_date: dt.date) -> List[Dict[str, str]]:
    """The price table as a list of dicts keyed by ``COLUMNS``, values still text.

    An empty list means FedInvest published no prices for that date. A page for a different
    date, a changed header or a short row raises ``FedInvestFormatError``.
    """
    shown = page_date(html)
    if shown is not None and shown != price_date:
        raise FedInvestFormatError(f"asked for {price_date}, the page is for {shown}")
    parser = _TableParser()
    parser.feed(html)
    if not parser.found:
        return []
    if not parser.rows or parser.rows[0] != HEADER:
        raise FedInvestFormatError(f"unexpected header: {parser.rows[:1]}")
    out = []
    for row in parser.rows[1:]:
        if len(row) != len(COLUMNS):
            raise FedInvestFormatError(f"row with {len(row)} cells: {row}")
        out.append(dict(zip(COLUMNS, row)))
    return out


def parse_us_date(text: str) -> Optional[dt.date]:
    text = text.strip()
    return dt.datetime.strptime(text, "%m/%d/%Y").date() if text else None


def parse_rate(text: str) -> float:
    return float(text.strip().rstrip("%"))


def security_from_row(row: Dict[str, str], static: Optional[Dict[str, object]] = None) -> Security:
    """A ``Security`` from one FedInvest row, with the auction record's dates when there is one."""
    kind = KINDS.get(row["security_type"])
    if kind is None:
        raise FedInvestFormatError(f"unknown security type {row['security_type']!r}")
    static = static or {}
    return Security(
        cusip=row["cusip"],
        kind=kind,
        coupon=parse_rate(row["rate"]),
        maturity=parse_us_date(row["maturity_date"]),
        dated_date=static.get("dated_date"),
        first_coupon_date=static.get("first_coupon_date"),
        first_period=static.get("first_period"),
        issue_date=static.get("issue_date"),
        original_term=static.get("original_term"),
    )
