"""The National Treasury's fixed-rate bond auction results, as rows.

The Treasury publishes one spreadsheet per fiscal year (April to March) under Historical
Results/Fixed-rate bonds/. Three layouts appear over the 16 files:

- ``2010-13.xls``: one long table, a row per bond auctioned, with the redemption date in full
  and amounts in R million. It has no best or worst bid.
- every later file: one sheet per month (2013-14 puts the year on one sheet), one column per
  bond auctioned, the labels down the first or second column, and the auction and settlement
  dates written once over each auction's first column.
- yields as percent (8.63) in most sheets, as fractions (0.0863) in the 2012-13 to 2014-15
  files.

The parser takes a sheet as a grid of cells, so the same code reads ``.xls`` through xlrd and
``.xlsx`` through openpyxl. It never changes a published figure. Yields stated as fractions are
multiplied by 100, with ``yield_unit`` recording it; amounts are in rand, so the long table's
R million figures are multiplied by a million. Every check that fails becomes a flag on the row:

- ``clearing_outside_bids``: the clearing yield is not between the best and the worst bid, as
  R2053's 1.85 on 2025-04-22 against bids of 11.76 to 12.00;
- ``no_bid_range``: the file gives no best and worst bid to check it against;
- ``bond_code_unrecognised``: the bond column holds no bond code (one row in the long table
  reads ``0``);
- ``date_outside_fiscal_year``: neither the auction nor the settlement date falls in the fiscal
  year the file covers, as the 2018-19 file's January 2018 auctions, dated on Mondays, whose day
  and month are Tuesdays in 2019. The yield may be right, but its date is not, so it stays out
  of the curve, and it never replaces another row for the same date;
- ``coupon_differs_from_schedule`` and ``maturity_year_differs_from_schedule``: the auction
  sheet's "(coupon; redemption)" disagrees with the Treasury's debt schedule;
- ``not_in_schedule``: no debt schedule lists the bond;
- ``before_issue_date`` and ``misdated_auction``: a bond settles before the debt schedule's
  issue date for it, so the auction's date is wrong, and every bond in that auction is marked.
  The 2012-13 file's February and March 2013 sheets sell R2030 and R2037, first issued in
  October and July 2013, at yields near 9% when the long table has the same weeks near 8.3%;
- ``settlement_date_doubtful``: the settlement date is not one to ten days after the auction, as
  the 2014-07-08 auction "settling" on 2014-06-11;
- ``files_disagree``: two files hold the same auction date with different bonds or yields. The
  2010-13 long table and the 2012-13 file share a year and differ on 8 of its 44 dates: on
  2012-08-21 the long table has R2048 at 8.40 and the yearly file 9.50; on 2013-03-05 the yearly
  file gives R2023 R214's 8.295. Neither can be taken over the other, so both stay out;
- ``off_neighbouring_auctions``: the yield sits more than 50 basis points from the same bond's
  auctions on either side, on the same side of both, while those two agree within 50 basis
  points, as R2048's 9.50 on 2012-08-21 between 8.31 and 8.53, and the same day's other bonds
  did not move half as far the same way. A move the market made shows in the next auction or
  across the curve; a spike in one bond that comes straight back is a wrong figure or a wrong bond.

``flag`` runs before ``deduplicate``, so a misdated row never replaces a row for the same date.

Rows whose yield fails a check stay in the table and stay out of the curve. Standard library
only, apart from the two spreadsheet readers, which are imported where a file is opened.
"""

from __future__ import annotations

import datetime as dt
import io
import re
from typing import Dict, Iterable, List, Optional, Sequence

SOURCE = "National Treasury of the Republic of South Africa"
# Flags that keep a row's yield out of the curve.
BLOCKING = {
    "clearing_outside_bids", "bond_code_unrecognised", "not_in_schedule", "no_yield", "date_outside_fiscal_year",
    "off_neighbouring_auctions", "before_issue_date", "misdated_auction", "settlement_date_doubtful",
    "files_disagree",
}
MISDATED = {"date_outside_fiscal_year", "misdated_auction"}
SPIKE = 0.50  # percentage points
SPIKE_WINDOW_DAYS = 42

_LABELS = {
    "auction_date": ("auction date",),
    "settle_date": ("settlement date",),
    "bond": ("bonds auctioned", "bond auctioned"),
    "terms": ("bond coupon",),
    "offered": ("amount on offer",),
    "allocated": ("total amount allocated",),
    "non_comps": ("non comps",),
    "bids_received": ("total amount of bids received",),
    "number_of_bids": ("total number of bids received",),
    "clearing_yield": ("clearing yield",),
    "best_bid": ("best bid",),
    "worst_bid": ("worst bid",),
    "bid_to_cover": ("bid to cover",),
}


def fiscal_range(source_file: str) -> Optional[tuple]:
    """(first, last) day a file covers, from the years in its name: ``2010-13`` is April 2010 to March 2013."""
    m = re.search(r"(\d{4})-(\d{2})\.xlsx?$", source_file or "")
    if not m:
        return None
    first = int(m.group(1))
    last = first // 100 * 100 + int(m.group(2))
    if last < first:
        last += 100
    return dt.date(first, 4, 1), dt.date(last, 3, 31)


# --- cells --------------------------------------------------------------------------------------


def _text(v) -> str:
    return "" if v is None else str(v).strip()


def _number(v) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(" ", "").replace(",", "")
    if s.endswith("%"):
        s = s[:-1]
    try:
        return float(s)
    except ValueError:
        return None


def _date(v) -> Optional[dt.date]:
    if v is None:
        return None
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    s = re.sub(r"\s+", " ", str(v).strip())
    for fmt in ("%d %B %Y", "%d %b %Y", "%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def bond_code(label) -> Optional[str]:
    """``R2030`` from ``R2030``, ``2030``, ``R'2030`` or the integer 213; None for anything else.

    The label as published stays on the row as ``bond_label``.
    """
    if isinstance(label, float) and label.is_integer():
        label = int(label)
    s = re.sub(r"[\s'’`]", "", _text(label).upper())
    if re.fullmatch(r"R?\d{3,4}", s) and s.lstrip("R") != "0":
        return s if s.startswith("R") else "R" + s
    return None


def parse_terms(text) -> tuple:
    """(coupon percent, redemption year) from any of "(7%;2031)", "7.00%, 2031", "8%; 2030"."""
    s = _text(text)
    c = re.search(r"(\d+(?:\.\d+)?)\s*%", s)
    y = re.search(r"(?<!\d)((?:19|20)\d{2})(?!\d)", s)
    return (float(c.group(1)) if c else None, int(y.group(1)) if y else None)


# --- the two layouts ----------------------------------------------------------------------------


def _is_long_table(grid: Sequence[Sequence]) -> bool:
    return bool(grid) and _text(grid[0][0] if grid[0] else "").lower().startswith("bond auctioned")


def _parse_long(grid: Sequence[Sequence], source_file: str, sheet: str) -> List[dict]:
    head = [_text(h).lower() for h in grid[0]]

    def col(prefix):
        return next(i for i, h in enumerate(head) if h.startswith(prefix))

    c = {
        "bond": col("bond auctioned"), "coupon": col("bond coupon"), "redemption": col("redemption"),
        "auction": col("auction date"), "settle": col("settlement date"), "offered": col("offer amount"),
        "allocated": col("allocated amount"), "clearing": col("clearing yield"),
        "bids": col("total number of bids received"), "cover": col("bid-to-cover"), "non_comps": col("non comps"),
    }
    million = 1_000_000.0
    rows = []
    for r in grid[1:]:
        cell = lambda k: r[c[k]] if c[k] < len(r) else None  # noqa: E731
        auction = _date(cell("auction"))
        if auction is None:
            continue
        redemption = _date(cell("redemption"))

        def rand(k):
            v = _number(cell(k))
            return None if v is None else v * million

        rows.append({
            "auction_date": auction, "settle_date": _date(cell("settle")), "bond_label": _text(cell("bond")),
            "coupon_quoted": _number(cell("coupon")), "redemption_year_quoted": redemption.year if redemption else None,
            "offered_rand": rand("offered"), "allocated_rand": rand("allocated"), "non_comps_rand": rand("non_comps"),
            "bids_received_rand": rand("bids"), "number_of_bids": None, "clearing_yield": _number(cell("clearing")),
            "best_bid": None, "worst_bid": None, "bid_to_cover": _number(cell("cover")), "yield_unit": "percent",
            "source_file": source_file, "sheet": sheet, "_code_cell": cell("bond"),
        })
    return rows


def _label_column(grid: Sequence[Sequence]) -> Optional[int]:
    for r in grid:
        for j, v in enumerate(r[:3]):
            if _text(v).lower().startswith("auction date"):
                return j
    return None


def _parse_wide(grid: Sequence[Sequence], source_file: str, sheet: str) -> List[dict]:
    lc = _label_column(grid)
    if lc is None:
        return []
    starts = [i for i, r in enumerate(grid) if len(r) > lc and _text(r[lc]).lower().startswith("auction date")]
    rows = []
    for b, start in enumerate(starts):
        end = starts[b + 1] if b + 1 < len(starts) else len(grid)
        where: Dict[str, int] = {}
        for i in range(start, end):
            label = _text(grid[i][lc] if len(grid[i]) > lc else "").lower()
            for name, prefixes in _LABELS.items():
                if name not in where and label.startswith(prefixes):
                    where[name] = i
        if "bond" not in where or "clearing_yield" not in where:
            continue
        width = max(len(grid[i]) for i in range(start, end))

        def cell(name, j):
            i = where.get(name)
            if i is None or j >= len(grid[i]):
                return None
            return grid[i][j]

        block = []
        auction = settle = None
        for j in range(lc + 1, width):
            if _date(cell("auction_date", j)) is not None:
                auction = _date(cell("auction_date", j))
                settle = _date(cell("settle_date", j))
            label = cell("bond", j)
            if _text(label) == "" or auction is None:
                continue
            coupon, year = parse_terms(cell("terms", j))
            block.append({
                "auction_date": auction, "settle_date": settle, "bond_label": _text(label),
                "coupon_quoted": coupon, "redemption_year_quoted": year,
                "offered_rand": _number(cell("offered", j)), "allocated_rand": _number(cell("allocated", j)),
                "non_comps_rand": _number(cell("non_comps", j)), "bids_received_rand": _number(cell("bids_received", j)),
                "number_of_bids": _number(cell("number_of_bids", j)), "clearing_yield": _number(cell("clearing_yield", j)),
                "best_bid": _number(cell("best_bid", j)), "worst_bid": _number(cell("worst_bid", j)),
                "bid_to_cover": _number(cell("bid_to_cover", j)), "source_file": source_file, "sheet": sheet,
                "_code_cell": label,
            })
        # A sheet states its yields as fractions or as percent, never both.
        ys = [v for r in block for v in (r["clearing_yield"], r["best_bid"], r["worst_bid"]) if v]
        fraction = bool(ys) and sorted(ys)[len(ys) // 2] < 1.0
        for r in block:
            r["yield_unit"] = "fraction" if fraction else "percent"
            if fraction:
                for k in ("clearing_yield", "best_bid", "worst_bid"):
                    if r[k] is not None:
                        r[k] = round(r[k] * 100.0, 10)
        rows.extend(block)
    return rows


def parse_grid(grid: Sequence[Sequence], source_file: str = "", sheet: str = "") -> List[dict]:
    """Every bond auctioned on one sheet, unflagged; ``flag`` adds the checks."""
    rows = _parse_long(grid, source_file, sheet) if _is_long_table(grid) else _parse_wide(grid, source_file, sheet)
    for r in rows:
        r["bond_code"] = bond_code(r.pop("_code_cell"))
        r["source"] = SOURCE
    return rows


# --- files --------------------------------------------------------------------------------------


def read_workbook(body: bytes, name: str) -> Dict[str, List[list]]:
    """{sheet name: grid} for an ``.xls`` (xlrd) or ``.xlsx`` (openpyxl) file's bytes."""
    if name.lower().endswith(".xlsx"):
        import openpyxl

        wb = openpyxl.load_workbook(io.BytesIO(body), read_only=True, data_only=True)
        out = {ws.title: [list(r) for r in ws.iter_rows(values_only=True)] for ws in wb.worksheets}
        wb.close()
        return out
    import xlrd

    book = xlrd.open_workbook(file_contents=body)
    out = {}
    for sh in book.sheets():
        grid = []
        for i in range(sh.nrows):
            row = []
            for j in range(sh.ncols):
                c = sh.cell(i, j)
                if c.ctype == xlrd.XL_CELL_DATE:
                    row.append(xlrd.xldate.xldate_as_datetime(c.value, book.datemode))
                elif c.ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
                    row.append(None)
                else:
                    row.append(c.value)
            grid.append(row)
        out[sh.name] = grid
    return out


def parse_file(body: bytes, name: str) -> List[dict]:
    """Every sheet of one file, each row flagged when its dates fall outside the file's fiscal year."""
    rows = []
    for sheet, grid in read_workbook(body, name).items():
        rows.extend(parse_grid(grid, name, sheet.strip()))
    span = fiscal_range(name)
    for r in rows:
        inside = span and any(d is not None and span[0] <= d <= span[1] for d in (r["auction_date"], r["settle_date"]))
        r["quality_flags"] = "" if inside or span is None else "date_outside_fiscal_year"
    return rows


# --- checks -------------------------------------------------------------------------------------


def flag(rows: Iterable[dict], schedule: Dict[str, dict]) -> List[dict]:
    """Add ``quality_flags`` to each row: the bid range, the code, the debt schedule's terms and issue
    date. A row's ``date_outside_fiscal_year`` from ``parse_file`` is kept."""
    out = []
    for r in rows:
        flags = [f for f in (r.get("quality_flags") or "").split(",") if f == "date_outside_fiscal_year"]
        y, best, worst = r["clearing_yield"], r["best_bid"], r["worst_bid"]
        if y is None:
            flags.append("no_yield")
        elif best is None or worst is None:
            flags.append("no_bid_range")
        elif not (min(best, worst) <= y <= max(best, worst)):
            flags.append("clearing_outside_bids")
        code = r["bond_code"]
        if code is None:
            flags.append("bond_code_unrecognised")
        elif code not in schedule:
            flags.append("not_in_schedule")
        else:
            st = schedule[code]
            if r["coupon_quoted"] is not None and abs(r["coupon_quoted"] - st["coupon"]) > 1e-9:
                flags.append("coupon_differs_from_schedule")
            years = {d.year for d in st["redemptions"]}
            if r["redemption_year_quoted"] is not None and r["redemption_year_quoted"] not in years:
                flags.append("maturity_year_differs_from_schedule")
            issued = st.get("issue_date")
            if issued is not None and r["settle_date"] is not None and r["settle_date"] < issued:
                flags.append("before_issue_date")
        gap = (r["settle_date"] - r["auction_date"]).days if r["settle_date"] else None
        if gap is None or not 1 <= gap <= 10:
            flags.append("settlement_date_doubtful")
        out.append({**r, "quality_flags": ",".join(flags)})
    bad = {(r["source_file"], r["sheet"], r["auction_date"]) for r in out if "before_issue_date" in r["quality_flags"]}
    for r in out:
        if (r["source_file"], r["sheet"], r["auction_date"]) in bad:
            r["quality_flags"] = ",".join(_flags(r) + ["misdated_auction"])
    return _cross_check_files(out)


def _cross_check_files(rows: List[dict]) -> List[dict]:
    """``files_disagree`` on every row of an auction date two files hold with different figures."""
    by_date: Dict[dt.date, Dict[str, Dict[str, Optional[float]]]] = {}
    for r in rows:
        if set(_flags(r)) & MISDATED:
            continue
        by_date.setdefault(r["auction_date"], {}).setdefault(r["source_file"], {})[r["bond_code"] or r["bond_label"]] = r["clearing_yield"]
    disputed = {d for d, files in by_date.items() if len(files) > 1 and len({tuple(sorted(m.items())) for m in files.values()}) > 1}
    for r in rows:
        if r["auction_date"] in disputed and not (set(_flags(r)) & MISDATED):
            r["quality_flags"] = ",".join(_flags(r) + ["files_disagree"])
    return rows


def deduplicate(rows: Iterable[dict]) -> List[dict]:
    """One row per auction date and bond: a later row replaces an earlier one, so a revised file wins,
    unless the later row has no best and worst bid and the earlier one does.

    That keeps the yearly 2012-13 file's rows over the 2010-13 long table's for the year both hold:
    only the yearly file carries the bids.
    """
    best: Dict[tuple, dict] = {}
    for r in rows:
        misdated = bool(set(_flags(r)) & MISDATED)
        k = (r["auction_date"], r["bond_code"] or r["bond_label"], misdated)
        cur = best.get(k)
        if cur is None or not (cur["best_bid"] is not None and r["best_bid"] is None):
            best[k] = r
    return sorted(best.values(), key=lambda r: (r["auction_date"], r["bond_code"] or r["bond_label"], r.get("source_file") or ""))


def _flags(r: dict) -> List[str]:
    return [f for f in (r.get("quality_flags") or "").split(",") if f]


def flag_neighbours(rows: List[dict]) -> List[dict]:
    """Set ``off_neighbouring_auctions`` afresh over every row: each bond's auctions in date order,
    leaving out rows another check already keeps out of the curve."""
    out = []
    for r in rows:
        r = dict(r)
        r["quality_flags"] = ",".join(f for f in _flags(r) if f != "off_neighbouring_auctions")
        out.append(r)
    by_bond: Dict[str, List[dict]] = {}
    for r in out:
        if r["bond_code"] and r["clearing_yield"] is not None and not (set(_flags(r)) & BLOCKING):
            by_bond.setdefault(r["bond_code"], []).append(r)
    moves: Dict[tuple, float] = {}  # (date, bond): the change since the bond's previous auction
    for series in by_bond.values():
        series.sort(key=lambda r: r["auction_date"])
        for prev, cur in zip(series, series[1:]):
            moves[(cur["auction_date"], cur["bond_code"])] = cur["clearing_yield"] - prev["clearing_yield"]
    for series in by_bond.values():
        for prev, cur, nxt in zip(series, series[1:], series[2:]):
            if (nxt["auction_date"] - prev["auction_date"]).days > 2 * SPIKE_WINDOW_DAYS:
                continue
            up, down = cur["clearing_yield"] - prev["clearing_yield"], cur["clearing_yield"] - nxt["clearing_yield"]
            if not (abs(up) > SPIKE and abs(down) > SPIKE and (up > 0) == (down > 0)
                    and abs(prev["clearing_yield"] - nxt["clearing_yield"]) < SPIKE):
                continue
            others = sorted(m for (d, b), m in moves.items() if d == cur["auction_date"] and b != cur["bond_code"])
            if others and abs(others[len(others) // 2]) >= SPIKE / 2 and (others[len(others) // 2] > 0) == (up > 0):
                continue  # the curve moved that day
            cur["quality_flags"] = ",".join(_flags(cur) + ["off_neighbouring_auctions"])
    return out


def usable(r: dict) -> bool:
    """A row whose clearing yield goes into the curve."""
    flags = set(filter(None, (r.get("quality_flags") or "").split(",")))
    return r.get("clearing_yield") is not None and not (flags & BLOCKING)
