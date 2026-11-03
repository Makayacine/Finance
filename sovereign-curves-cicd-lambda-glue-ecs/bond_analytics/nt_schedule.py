"""Bond static from the National Treasury's "Schedule of domestic government bonds as at <date>".

One PDF a month under Schedule of Domestic Debt/. Each bond is one text line:

    R2030 8.00% 04-Oct-13 95.261%   300 000 000 28/02 & 31/08 31-Jan-30 297 978 110 031

code, coupon, initial issue date, initial issue price, initial amount, interest dates (day and
month), redemption date and the nominal amount outstanding. A three-legged bond (R186, R2044,
R2048) is listed once per leg, each with a third of the nominal, under one code before it was
split and under three codes after it.

Only fixed-rate bonds are kept: an ``R`` and digits, a coupon in percent, and not marked ``**``
(inflation-linked). FRNs (``RN``), the retail and private placements (``RS``, ``RI``) and the
perpetuals have no place in the auction curve.

The schedule is not edited. Where its interest dates do not include the day and month of its own
redemption date, as R2030's 28/02 & 31/08 against 31-Jan-30, the coupon cycle is taken from the
redemption date, which a bond's final coupon falls on, and the bond is flagged
``interest_dates_disagree_with_redemption``. Standard library only; the text comes from pypdf.
"""

from __future__ import annotations

import datetime as dt
import io
import re
from typing import Dict, Iterable, List, Optional, Tuple

SOURCE = "National Treasury of the Republic of South Africa"
_LINE = re.compile(
    r"^(?P<code>R\d{3,4})(?P<marks>[*/]*)\s+(?P<coupon>\d+(?:\.\d+)?)%\s+(?P<issue>\d{1,2}-[A-Za-z]{3}-\d{2})\s+"
    r"(?:(?P<price>\d+(?:\.\d+)?)%|-)?\s*(?P<rest>.*)$"
)
_DATE = re.compile(r"\d{1,2}-[A-Za-z]{3}-\d{2}")
_DAYMONTH = re.compile(r"(\d{1,2})/(\d{1,2})")
_AS_AT = re.compile(r"as at (\d{1,2}) ([A-Za-z]+) (\d{4})", re.I)


def _d(text: str) -> dt.date:
    return dt.datetime.strptime(text, "%d-%b-%y").date()


def schedule_date(name_or_text: str) -> Optional[dt.date]:
    """The "as at" date in a file name or a page heading; NT once spelt January "Janaury"."""
    m = _AS_AT.search(name_or_text.replace("Janaury", "January"))
    if not m:
        return None
    return dt.datetime.strptime(" ".join(m.groups()), "%d %B %Y").date()


def pdf_text(body: bytes) -> str:
    from pypdf import PdfReader

    return "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(body)).pages)


def parse_lines(text: str) -> List[dict]:
    """One dict per fixed-rate bond line (one per leg for a three-legged bond)."""
    out = []
    for line in text.splitlines():
        m = _LINE.match(line.strip())
        if not m or "**" in m.group("marks"):
            continue
        rest = m.group("rest")
        dates = _DATE.findall(rest)
        if not dates:
            continue
        redemption = dates[-1]
        before, after = rest.rsplit(redemption, 1)
        pairs = [(int(d), int(mo)) for d, mo in _DAYMONTH.findall(before)]
        nominal = re.sub(r"[^\d.]", "", after.split(".")[0] if after.count(".") else after)
        out.append({
            "bond_code": m.group("code"), "coupon": float(m.group("coupon")), "issue_date": _d(m.group("issue")),
            "issue_price": float(m.group("price")) if m.group("price") else None, "interest_dates": pairs,
            "redemption": _d(redemption), "nominal_rand": float(nominal) if nominal else None,
        })
    return out


def _cycle(redemption: dt.date, listed: List[Tuple[int, int]]) -> Tuple[Tuple[int, int], Tuple[int, int], bool]:
    """((month, day), (month, day)) of the two coupons, and whether the listed dates were used."""
    if (redemption.day, redemption.month) in listed and len(listed) == 2:
        (d1, m1), (d2, m2) = sorted(listed, key=lambda p: (p[1], p[0]))
        return (m1, d1), (m2, d2), True
    other = (redemption.month + 5) % 12 + 1
    from calendar import monthrange

    month_end = redemption.day == monthrange(redemption.year, redemption.month)[1]
    day = 31 if month_end else redemption.day
    pair = sorted([(redemption.month, redemption.day), (other, day)])
    return pair[0], pair[1], False


def bonds(lines: Iterable[dict], as_at: Optional[dt.date]) -> Dict[str, dict]:
    """One record per bond code from one schedule's lines: legs gathered, the coupon cycle set."""
    by_code: Dict[str, List[dict]] = {}
    for ln in lines:
        by_code.setdefault(ln["bond_code"], []).append(ln)
    out = {}
    for code, legs in by_code.items():
        legs = sorted(legs, key=lambda x: x["redemption"])
        redemptions = [x["redemption"] for x in legs]
        mid = redemptions[len(redemptions) // 2]  # a three-legged bond is priced on its middle leg
        (m1, d1), (m2, d2), listed_ok = _cycle(mid, legs[0]["interest_dates"])
        flags = [] if listed_ok else ["interest_dates_disagree_with_redemption"]
        out[code] = {
            "bond_code": code, "coupon": legs[0]["coupon"], "issue_date": legs[0]["issue_date"],
            "issue_price": legs[0]["issue_price"],
            "interest_dates_listed": " & ".join(f"{d:02d}/{m:02d}" for d, m in legs[0]["interest_dates"]),
            "coupon_months": f"{m1},{m2}", "coupon_days": f"{d1},{d2}",
            "redemptions": redemptions, "pricing_maturity": mid, "legs": len(legs),
            "nominal_rand": sum(x["nominal_rand"] or 0.0 for x in legs),  # each leg's line is its own third
            "schedule_date": as_at, "quality_flags": ",".join(flags), "source": SOURCE,
        }
    return out


def merge(schedules: Iterable[Dict[str, dict]]) -> Dict[str, dict]:
    """The union over several schedules, each code taken from the latest schedule that lists it."""
    out: Dict[str, dict] = {}
    for sched in schedules:
        for code, rec in sched.items():
            cur = out.get(code)
            if cur is None or (rec["schedule_date"] or dt.date.min) >= (cur["schedule_date"] or dt.date.min):
                out[code] = rec
    return out


def parse_pdf(body: bytes, name: str = "") -> Dict[str, dict]:
    text = pdf_text(body)
    return bonds(parse_lines(text), schedule_date(name) or schedule_date(text))
