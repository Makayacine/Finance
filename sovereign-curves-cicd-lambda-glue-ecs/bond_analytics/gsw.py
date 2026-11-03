"""The Federal Reserve Board's Svensson curve (Gurkaynak, Sack and Wright), feds200628.csv.

The file opens with a note and a table of series before its header row, which starts ``Date,``.
Before 1980 the fit is Nelson-Siegel: BETA3 is 0 and TAU2 is -999.99. Missing values are
``NA``. Standard library only.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
from typing import Dict, Iterator, Optional

PARAMS = ("BETA0", "BETA1", "BETA2", "BETA3", "TAU1", "TAU2")
ZERO_TENORS = tuple(range(1, 31))  # SVENY01 .. SVENY30


def _number(text: str) -> Optional[float]:
    text = text.strip()
    if text in ("", "NA"):
        return None
    return float(text)


def read_rows(text: str) -> Iterator[Dict[str, object]]:
    """One dict per date: ``date``, the six parameters and the 30 zero-coupon yields."""
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith("Date,")), None)
    if start is None:
        raise ValueError("no header row starting 'Date,' in the GSW file")
    reader = csv.DictReader(io.StringIO("\n".join(lines[start:])))
    for rec in reader:
        if not rec.get("Date"):
            continue
        row: Dict[str, object] = {"date": dt.date.fromisoformat(rec["Date"])}
        for p in PARAMS:
            row[p.lower()] = _number(rec[p])
        if row["tau2"] is not None and row["tau2"] < 0:  # Nelson-Siegel years
            row["tau2"] = None
            row["beta3"] = 0.0
        for n in ZERO_TENORS:
            row[f"sveny{n:02d}"] = _number(rec.get(f"SVENY{n:02d}", "NA"))
        yield row


def params_of(row: Dict[str, object]) -> tuple:
    """(beta0, beta1, beta2, beta3, tau1, tau2) in the units the file uses: percent and years."""
    return tuple(row[p.lower()] for p in PARAMS)
