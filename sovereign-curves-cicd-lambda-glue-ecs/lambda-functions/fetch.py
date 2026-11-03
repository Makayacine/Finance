"""Fetch the day's raw sources into S3, then start the Glue load for that date.

Started each weekday evening by an EventBridge schedule. Four sources, each fetched on its own so
that one failing never stops the others:

- ``fedinvest``: the Treasury's FedInvest end-of-day prices. The page wants the ``_csrf``
  token and session cookie from a GET before it accepts the POST of ``priceDate``; a bare POST
  gets 403. One POST per date.
- ``gsw``: the Federal Reserve Board's Svensson curve file, ``feds200628.csv``, whole.
- ``auctions``: the Treasury's auction records from the Fiscal Data API, the last 14 days of
  them, which carry each new CUSIP's issue and dated dates and its original term.
- ``nyfed``: SOFR, BGCR and TGCR from the New York Fed's markets API.

Each source lands as one object per date under ``raw/<source>/date=<YYYY-MM-DD>/`` as it was
received. A source that fails is reported in the response and logged, and the Glue job is still
started for the ones that arrived; the job is not started when none did.

Standard library and boto3 only, so the deployment package is this one file.
"""

from __future__ import annotations

import datetime as dt
import http.cookiejar
import json
import logging
import os
import re
import time
import urllib.parse
import urllib.request
from pathlib import PurePosixPath
from typing import Callable, Dict, Optional, Tuple

FEDINVEST_URL = "https://www.treasurydirect.gov/GA-FI/FedInvest/selectSecurityPriceDate"
GSW_URL = "https://www.federalreserve.gov/data/yield-curve-tables/feds200628.csv"
AUCTIONS_URL = "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v1/accounting/od/auctions_query"
NYFED_URL = "https://markets.newyorkfed.org/api/rates/secured/all/search.json"
AUCTION_FIELDS = (
    "cusip,security_type,original_security_term,auction_date,issue_date,dated_date,maturity_date,"
    "int_rate,first_int_period,first_int_payment_date,reopening,inflation_index_security,floating_rate"
)
USER_AGENT = "sovereign-curves-fetch/1.0"
TIMEOUT = 60

log = logging.getLogger()
log.setLevel(logging.INFO)

_CSRF = re.compile(r'name="_csrf"\s+value="([^"]+)"')


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


def fetch_nyfed(day: dt.date, opener=None) -> bytes:
    opener = opener or _opener()
    query = urllib.parse.urlencode({"startDate": day.isoformat(), "endDate": day.isoformat()})
    with opener.open(f"{NYFED_URL}?{query}", timeout=TIMEOUT) as resp:
        data = resp.read()
    json.loads(data)["refRates"]
    return data


SOURCES: Dict[str, Tuple[Callable, str]] = {
    "fedinvest": (fetch_fedinvest, "prices.html"),
    "gsw": (fetch_gsw, "feds200628.csv"),
    "auctions": (fetch_auctions, "auctions.json"),
    "nyfed": (fetch_nyfed, "rates.json"),
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
            key = raw_key(name, day, filename)
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
