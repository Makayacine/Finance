"""The two monthly series the South African auction curve is checked against.

- **OECD**, Financial market dataset, long-term interest rates for South Africa (``IRLT``),
  monthly, through the OECD's SDMX API with no key. Its metadata names the South African Reserve
  Bank as the source and describes the series as government bonds with maturities of 10 years
  and over. OECD terms allow any use with the citation below.
- **IMF**, International Financial Statistics, government bond yield for South Africa, monthly,
  as FRED distributes it (series ``INTGSBZAM193N``). IMF terms allow publishing with the source
  named; commercial reuse needs the IMF's permission.

Both are percent a year. One row per month, dated the first of the month, as the series date
themselves. Standard library only.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
from typing import Dict, List

OECD_URL = (
    "https://sdmx.oecd.org/public/rest/data/OECD.SDD.STES,DSD_STES@DF_FINMARK,4.0/ZAF.M.IRLT.PA....."
    "?startPeriod=2010-01&format=csvfilewithlabels"
)
IMF_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=INTGSBZAM193N&cosd=2010-01-01"

CITATIONS = {
    "oecd_irlt": "OECD ({year}), Financial market (DSD_STES@DF_FINMARK), long-term interest rates, South Africa, "
                 "South African Reserve Bank, https://sdmx.oecd.org/public/rest/data/OECD.SDD.STES,DSD_STES@DF_FINMARK,4.0/"
                 "ZAF.M.IRLT.PA..... (accessed on {accessed})",
    "imf_ifs": "Source: International Monetary Fund, International Financial Statistics, government bond yield "
               "for South Africa, via FRED series INTGSBZAM193N, https://fred.stlouisfed.org/series/INTGSBZAM193N",
}

COLUMNS = ["date", "series", "percent", "source"]


def _month(text: str) -> dt.date:
    return dt.date.fromisoformat(text[:7] + "-01")


def parse_oecd(body: bytes, accessed: dt.date) -> List[Dict[str, object]]:
    """Rows from the SDMX ``csvfilewithlabels`` file: TIME_PERIOD and OBS_VALUE for ZAF IRLT."""
    rows = []
    for rec in csv.DictReader(io.StringIO(body.decode("utf-8-sig"))):
        if rec.get("REF_AREA") != "ZAF" or rec.get("MEASURE") != "IRLT" or rec.get("OBS_VALUE") in (None, ""):
            continue
        rows.append({"date": _month(rec["TIME_PERIOD"]), "series": "oecd_irlt", "percent": float(rec["OBS_VALUE"]),
                     "source": CITATIONS["oecd_irlt"].format(year=accessed.year, accessed=accessed.isoformat())})
    if not rows:
        raise ValueError("no ZAF IRLT observations in the OECD file")
    return sorted(rows, key=lambda r: r["date"])


def parse_imf(body: bytes) -> List[Dict[str, object]]:
    """Rows from FRED's two-column CSV; a missing month is ``.`` or empty and is left out."""
    reader = csv.reader(io.StringIO(body.decode("utf-8-sig")))
    header = next(reader)
    if len(header) != 2 or header[1] != "INTGSBZAM193N":
        raise ValueError(f"unexpected FRED header {header}")
    rows = []
    for date, value in reader:
        if value in ("", "."):
            continue
        rows.append({"date": _month(date), "series": "imf_ifs", "percent": float(value), "source": CITATIONS["imf_ifs"]})
    return rows
