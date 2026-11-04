"""Fetch the day's raw sources into S3, then start the Glue load for that date.

Started each weekday evening by an EventBridge schedule. Eight sources, four for each market,
each fetched on its own so that one failing never stops the others. United States:

- ``fedinvest``: the Treasury's FedInvest end-of-day prices. The page wants the ``_csrf``
  token and session cookie from a GET before it accepts the POST of ``priceDate``; a bare POST
  gets 403. One POST per date.
- ``gsw``: the Federal Reserve Board's Svensson curve file, ``feds200628.csv``, whole.
- ``auctions``: the Treasury's auction records from the Fiscal Data API, the last 14 days of
  them, which carry each new CUSIP's issue and dated dates and its original term.
- ``nyfed``: SOFR, BGCR and TGCR from the New York Fed's markets API.

South Africa:

- ``nt_auctions``: the National Treasury's fixed-rate bond auction results for the fiscal year
  the date falls in, the whole spreadsheet as published.
- ``nt_schedule``: the latest "Schedule of domestic government bonds" PDF in the Treasury's
  folder, for each bond's coupon, interest dates and redemption.
- ``oecd``: the OECD's monthly long-term interest rate for South Africa, from 2010.
- ``imf``: the IMF's monthly South African government bond yield, as FRED distributes it.

Each source lands as one object per date under ``raw/<source>/date=<YYYY-MM-DD>/`` as it was
received. A source that fails is reported in the response and logged, and the Glue job is still
started for the ones that arrived; the job is not started when none did.

Standard library and boto3 only, so the deployment package is this one file.
"""

from __future__ import annotations

import datetime as dt
import html
import http.cookiejar
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import PurePosixPath
from typing import Callable, Dict, Optional, Tuple

FEDINVEST_URL = "https://www.treasurydirect.gov/GA-FI/FedInvest/selectSecurityPriceDate"
GSW_URL = "https://www.federalreserve.gov/data/yield-curve-tables/feds200628.csv"
AUCTIONS_URL = "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v1/accounting/od/auctions_query"
NYFED_URL = "https://markets.newyorkfed.org/api/rates/secured/all/search.json"
NT_ROOT = "https://investor.treasury.gov.za/Debt%20Operations%20and%20Data/"
NT_AUCTIONS_URL = NT_ROOT + "Historical%20Results/Fixed-rate%20bonds/"
NT_SCHEDULE_URL = NT_ROOT + "Schedule%20of%20Domestic%20Debt/"
OECD_URL = (
    "https://sdmx.oecd.org/public/rest/data/OECD.SDD.STES,DSD_STES@DF_FINMARK,4.0/ZAF.M.IRLT.PA....."
    "?startPeriod=2010-01&format=csvfilewithlabels"
)
IMF_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=INTGSBZAM193N&cosd=2010-01-01"
AUCTION_FIELDS = (
    "cusip,security_type,original_security_term,auction_date,issue_date,dated_date,maturity_date,"
    "int_rate,first_int_period,first_int_payment_date,reopening,inflation_index_security,floating_rate"
)
USER_AGENT = "sovereign-curves-fetch/1.0"
TIMEOUT = 60

log = logging.getLogger()
log.setLevel(logging.INFO)

_CSRF = re.compile(r'name="_csrf"\s+value="([^"]+)"')
_HREF = re.compile(r'href="([^"]+)"')
_AS_AT = re.compile(r"as at (\d{1,2}) ([A-Za-z]+) (\d{4})\.pdf$", re.I)
_FISCAL = re.compile(r"(\d{4}-\d{2})\.(xlsx?)$", re.I)
_MONTHS = {m: i for i, m in enumerate(
    ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november",
     "december"), start=1)}


class FetchError(RuntimeError):
    pass


def raw_key(source: str, day: dt.date, filename: str) -> str:
    return PurePosixPath("raw", source, f"date={day.isoformat()}", filename).as_posix()


def _opener() -> urllib.request.OpenerDirector:
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    opener.addheaders = [("User-Agent", USER_AGENT)]
    return opener


def fetch_fedinvest(day: dt.date, opener=None) -> bytes:
    """GET the form for its token and cookie, then POST the date in the same session."""
    opener = opener or _opener()
    with opener.open(FEDINVEST_URL, timeout=TIMEOUT) as resp:
        form = resp.read().decode("latin-1")
    m = _CSRF.search(form)
    if not m:
        raise FetchError("no _csrf token on the FedInvest form")
    body = urllib.parse.urlencode({"priceDate": day.isoformat(), "submit": "Show Prices", "_csrf": m.group(1)}).encode()
    with opener.open(urllib.request.Request(FEDINVEST_URL, data=body, method="POST"), timeout=TIMEOUT) as resp:
        return resp.read()


def fetch_gsw(day: dt.date, opener=None) -> bytes:
    opener = opener or _opener()
    with opener.open(GSW_URL, timeout=TIMEOUT) as resp:
        data = resp.read()
    if b"Date,BETA0" not in data[:4000]:
        raise FetchError("feds200628.csv has no 'Date,BETA0' header near its top")
    return data


def fetch_auctions(day: dt.date, opener=None, days_back: int = 14) -> bytes:
    opener = opener or _opener()
    query = urllib.parse.urlencode(
        {
            "fields": AUCTION_FIELDS,
            "filter": f"auction_date:gte:{(day - dt.timedelta(days=days_back)).isoformat()},auction_date:lte:{day.isoformat()}",
            "sort": "auction_date",
            "page[size]": "1000",
        },
        safe=":,[]",
    )
    with opener.open(f"{AUCTIONS_URL}?{query}", timeout=TIMEOUT) as resp:
        data = resp.read()
    json.loads(data)["data"]
    return data


def fetch_auction_history(opener=None) -> bytes:
    """Every auction record since 1979, for a backfill: about 11,000 records over two pages."""
    opener = opener or _opener()
    records = []
    page = 1
    while True:
        query = urllib.parse.urlencode(
            {"fields": AUCTION_FIELDS, "sort": "auction_date", "page[size]": "10000", "page[number]": str(page)},
            safe=":,[]",
        )
        with opener.open(f"{AUCTIONS_URL}?{query}", timeout=TIMEOUT) as resp:
            body = json.loads(resp.read())
        records.extend(body["data"])
        if page >= int(body["meta"]["total-pages"]):
            break
        page += 1
    return json.dumps({"data": records}).encode()


def fetch_nyfed(day: dt.date, opener=None, start: Optional[dt.date] = None) -> bytes:
    """The day's rates or, from ``start``, a whole range in one response: SOFR's 2,146 days to
    3 November 2026 came back as one 1.8 MB answer."""
    opener = opener or _opener()
    query = urllib.parse.urlencode({"startDate": (start or day).isoformat(), "endDate": day.isoformat()})
    with opener.open(f"{NYFED_URL}?{query}", timeout=TIMEOUT) as resp:
        data = resp.read()
    json.loads(data)["refRates"]
    return data


# --- South Africa -------------------------------------------------------------------------------


def nt_fiscal_year(day: dt.date) -> str:
    """``2026-27`` for any day from April 2026 to March 2027: the Treasury's file for that day."""
    start = day.year if day.month >= 4 else day.year - 1
    return f"{start}-{(start + 1) % 100:02d}"


def nt_auctions_name(day: dt.date) -> str:
    """The raw object's name: ``fixed-rate-bonds-2026-27.xlsx``. The Treasury writes .xlsx from 2020-21."""
    fy = nt_fiscal_year(day)
    return f"fixed-rate-bonds-{fy}.{'xlsx' if int(fy[:4]) >= 2020 else 'xls'}"


def _spreadsheet(data: bytes, what: str) -> bytes:
    if not (data[:4] == b"PK\x03\x04" or data[:8] == bytes.fromhex("d0cf11e0a1b11ae1")):
        raise FetchError(f"{what} is not a spreadsheet")
    return data


def _nt_get(url: str, opener, attempts: int = 3, wait: float = 2.0, sleep=time.sleep) -> bytes:
    """GET from the Treasury's site, which now and then resets a connection: three tries, 2 s then 4 s apart."""
    for i in range(attempts):
        try:
            with opener.open(url, timeout=TIMEOUT) as resp:
                return resp.read()
        except (urllib.error.URLError, ConnectionError) as exc:
            if isinstance(exc, urllib.error.HTTPError) or i == attempts - 1:
                raise
            log.warning("retrying %s after %s", url, exc)
            sleep(wait * 2**i)
    raise FetchError(url)  # not reached


def _listing(url: str, opener) -> list:
    page = _nt_get(url, opener).decode("utf-8", "replace")
    return [html.unescape(h) for h in _HREF.findall(page)]


def fetch_nt_auctions(day: dt.date, opener=None) -> bytes:
    opener = opener or _opener()
    ext = nt_auctions_name(day).rsplit(".", 1)[1]
    name = f"Fixed-rate bond auctions - {nt_fiscal_year(day)}.{ext}"
    return _spreadsheet(_nt_get(NT_AUCTIONS_URL + urllib.parse.quote(name), opener), name)


def fetch_nt_auction_history(opener=None, pause_seconds: float = 2.0, sleep=time.sleep) -> Dict[str, bytes]:
    """Every fiscal year's file in the Treasury's folder, from 2010-13: {raw object name: body}."""
    opener = opener or _opener()
    out = {}
    for href in _listing(NT_AUCTIONS_URL, opener):
        m = _FISCAL.search(href)
        if not m or "auctions" not in href.lower():
            continue
        if out:
            sleep(pause_seconds)
        name = href.rsplit("/", 1)[-1]
        body = _nt_get(NT_AUCTIONS_URL + urllib.parse.quote(name), opener)
        out[f"fixed-rate-bonds-{m.group(1)}.{m.group(2).lower()}"] = _spreadsheet(body, name)
    return out


def nt_schedules_listed(opener=None) -> Dict[dt.date, str]:
    """{as-at date: file name} for every schedule PDF in the Treasury's folder."""
    opener = opener or _opener()
    out = {}
    for href in _listing(NT_SCHEDULE_URL, opener):
        name = href.rsplit("/", 1)[-1]
        m = _AS_AT.search(name.replace("Janaury", "January"))
        if m and m.group(2).lower() in _MONTHS:
            out[dt.date(int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1)))] = name
    return out


def fetch_nt_schedule_as_at(name: str, opener=None) -> bytes:
    opener = opener or _opener()
    data = _nt_get(NT_SCHEDULE_URL + urllib.parse.quote(name), opener)
    if not data.startswith(b"%PDF"):
        raise FetchError(f"{name} is not a PDF")
    return data


def fetch_nt_schedule(day: dt.date, opener=None) -> bytes:
    """The newest schedule dated on or before ``day``."""
    opener = opener or _opener()
    listed = {d: n for d, n in nt_schedules_listed(opener).items() if d <= day}
    if not listed:
        raise FetchError(f"no debt schedule dated on or before {day}")
    return fetch_nt_schedule_as_at(listed[max(listed)], opener)


def fetch_oecd(day: dt.date, opener=None) -> bytes:
    opener = opener or _opener()
    with opener.open(OECD_URL, timeout=TIMEOUT) as resp:
        data = resp.read()
    if b"IRLT" not in data[:4000]:
        raise FetchError("the OECD file has no IRLT rows near its top")
    return data


def fetch_imf(day: dt.date, opener=None) -> bytes:
    opener = opener or _opener()
    with opener.open(IMF_URL, timeout=TIMEOUT) as resp:
        data = resp.read()
    if not data.lstrip(b"\xef\xbb\xbf").startswith(b"observation_date,INTGSBZAM193N"):
        raise FetchError("the FRED file does not start with its INTGSBZAM193N header")
    return data


SOURCES: Dict[str, Tuple[Callable, object]] = {
    "fedinvest": (fetch_fedinvest, "prices.html"),
    "gsw": (fetch_gsw, "feds200628.csv"),
    "auctions": (fetch_auctions, "auctions.json"),
    "nyfed": (fetch_nyfed, "rates.json"),
    "nt_auctions": (fetch_nt_auctions, nt_auctions_name),
    "nt_schedule": (fetch_nt_schedule, "schedule.pdf"),
    "oecd": (fetch_oecd, "irlt.csv"),
    "imf": (fetch_imf, "INTGSBZAM193N.csv"),
}


def fetch_all(day: dt.date, put: Callable[[str, bytes], None], sources=None, fetchers=None) -> Dict[str, str]:
    """Fetch every source for ``day`` and ``put`` each body under its raw key.

    Returns ``{source: "ok <key>" | "failed: <reason>"}``. One source's exception is caught,
    logged and reported; it never reaches the others.
    """
    fetchers = fetchers or SOURCES
    status = {}
    for name in sources or list(fetchers):
        fn, filename = fetchers[name]
        try:
            body = fn(day)
            key = raw_key(name, day, filename(day) if callable(filename) else filename)
            put(key, body)
            status[name] = f"ok {key}"
        except Exception as exc:  # isolate one source from the rest
            log.exception("source %s failed for %s", name, day)
            status[name] = f"failed: {type(exc).__name__}: {exc}"
    return status


def _price_date(event: dict) -> dt.date:
    if event.get("date_filter"):
        return dt.date.fromisoformat(event["date_filter"])
    return dt.datetime.now(dt.timezone.utc).date()


def lambda_handler(event, context, s3=None, glue=None):
    import boto3

    event = event or {}
    bucket = os.environ["RAW_BUCKET"]
    job = os.environ.get("GLUE_JOB_NAME")
    day = _price_date(event)
    s3 = s3 or boto3.client("s3")
    glue = glue or (boto3.client("glue") if job else None)

    def put(key: str, body: bytes) -> None:
        s3.put_object(Bucket=bucket, Key=key, Body=body)

    status = fetch_all(day, put, sources=event.get("sources"))
    arrived = [name for name, s in status.items() if s.startswith("ok")]
    run_id: Optional[str] = None
    if job and arrived:
        run_id = glue.start_job_run(
            JobName=job,
            Arguments={"--load_type": "incremental", "--date_filter": day.isoformat(), "--sources": ",".join(arrived)},
        )["JobRunId"]
    result = {"date": day.isoformat(), "sources": status, "glue_job_run_id": run_id}
    log.info(json.dumps(result))
    if not arrived:
        raise FetchError(f"no source arrived for {day}: {status}")
    return result


def backfill(days, put, pause_seconds: float = 2.0, sources=("fedinvest",), fetchers=None, sleep=time.sleep):
    """Fetch a run of dates one at a time, pausing between them: one request per price date."""
    out = {}
    for i, day in enumerate(days):
        if i:
            sleep(pause_seconds)
        out[day] = fetch_all(day, put, sources=sources, fetchers=fetchers)
    return out
