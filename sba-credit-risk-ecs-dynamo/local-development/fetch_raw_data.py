"""Fetch the SBA 7(a) loan book, the FDIC tables and the SBA recovery report into ``data/raw/``.

Runs on the host, once, before the pipeline. The validate and model containers only ever read
folders, so nothing past this script needs a network. Standard library only, so it runs from a
clean clone with nothing installed.

    data/raw/sba/FOIA_7a_*.csv                 SBA 7(a) FOIA loan book in three fiscal-year files
    data/raw/sba/7a_504_foia_data_dictionary.xlsx   the field definitions those files ship with
    data/raw/sba_recovery/                     Loan Program Performance report, 11 PDFs + the zip
    data/raw/fdic/failures.csv                 one row per failure or assistance event
    data/raw/fdic/institutions.csv             one row per CERT, active and closed
    data/raw/fdic/financials_prefailure.csv    every quarterly filing of every failed bank
    data/raw/fdic/financials_yearend.csv       every bank at every year-end, 1984-2025
    data/raw/manifest.json                     source, retrieval date, row count, sha256

The SBA 7(a) FOIA CSVs come from data.sba.gov/dataset/7a-504-foia, as of 2026-09-30. Each one is
streamed to a ``.part`` file and only renamed once its byte count matches the server's
Content-Length, so an interrupted run leaves no half file that looks whole. A file already on disk
at the right size is not downloaded again. They are 763 MB together and every one is over GitHub's
100 MB limit, which is why ``data/raw/`` is gitignored and a stratified sample is committed instead.
The CSVs carry borrower names and street addresses, some of them sole proprietors: those columns
are dropped on read by everything downstream and never leave ``data/raw/``.

WHY THERE ARE TWO FINANCIALS FILES
----------------------------------

They are two grains. The year-end panel is the PD grain: every bank, survivors included, one
snapshot a year. The run-off ratio needs the filing about a year before failure, and banks fail
in any quarter, so a year-end snapshot can sit up to 24 months back. Failed banks therefore get
their full quarterly history as well. Mixing the two in one file would quietly weight failed
banks four times in any PD fit that forgot to filter.

Sort order is unique within a page sequence on every paged pull, and each file refuses to save
if its row count disagrees with the API's own total or a key repeats.
"""

import csv
import hashlib
import http.client
import io
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import date
from pathlib import Path

FDIC_API = "https://api.fdic.gov/banks"
SBA_REPORT = "https://legacy.sba.gov/sites/default/files/2025-09/WebsiteReports_FY25Q3.zip"
SBA_REPORT_PDFS = 11
SBA_FOIA = "https://data.sba.gov/sites/default/files/uploaded_resources"
SBA_FOIA_FILES = (
    "FOIA_7a_FY2000_FY2009_asof_260930.csv",
    "FOIA_7a_FY2010_FY2019_asof_260930.csv",
    "FOIA_7a_FY2020_Present_asof_260930.csv",
    "7a_504_foia_data_dictionary.xlsx",
)
CHUNK_BYTES = 1 << 20

RAW = Path(__file__).resolve().parents[1] / "data" / "raw"

PAGE_SIZE = 10_000  # the API's maximum
CERTS_PER_BATCH = 100  # keeps a failed-bank batch on one page, so ties in the sort cannot matter
FIRST_YEAR, LAST_YEAR = 1984, 2025  # financials begin at 1984Q1; 2026 has no year-end yet
PAUSE_SECONDS = 0.2

FAILURE_FIELDS = (
    "CERT,NAME,CITY,PSTALP,FAILDATE,FAILYR,RESTYPE,RESTYPE1,SAVR,"
    "QBFDEP,QBFASSET,COST,COSTMOSTRECENTASOF,COMMENTS"
)
INSTITUTION_FIELDS = "CERT,NAME,CITY,STALP,BKCLASS,CHARTER,ACTIVE,ESTYMD,INSDATE,ENDEFYMD"
FINANCIAL_FIELDS = (
    "CERT,REPDTE,ASSET,DEP,DEPINS,DEPUNINS,LNLSNET,LNLSDEPR,"
    "EQV,ROA,NIMY,NCLNLSR,NTLNLSR,RBCRWAJ"
)


def get_json(url):
    for attempt in range(5):
        try:
            with urllib.request.urlopen(url, timeout=180) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if attempt == 4 or (error.code < 500 and error.code != 429):
                raise
        except (OSError, ValueError, http.client.HTTPException):
            if attempt == 4:
                raise
        time.sleep(2**attempt)


def fetch_rows(endpoint, fields, sort_by, filters=None):
    params = {"fields": fields, "limit": PAGE_SIZE, "sort_by": sort_by, "sort_order": "ASC"}
    if filters:
        params["filters"] = filters
    rows = []
    while True:
        query = urllib.parse.urlencode({**params, "offset": len(rows)})
        page = get_json(f"{FDIC_API}/{endpoint}?{query}")
        rows += [record["data"] for record in page["data"]]
        total = page["meta"]["total"]
        time.sleep(PAUSE_SECONDS)
        if len(rows) >= total or not page["data"]:
            break
    if len(rows) != total:
        raise RuntimeError(f"{endpoint} {filters}: fetched {len(rows):,} of {total:,} rows")
    return rows


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def save_csv(path, rows, fields, key):
    if len({tuple(row.get(column) for column in key) for row in rows}) != len(rows):
        raise RuntimeError(f"{path.name}: repeated {key}")
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_suffix(".part")
    with part.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fields.split(","), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(part, path)
    print(f"{path.name}: {len(rows):,} rows, {path.stat().st_size / 1e6:.1f} MB", flush=True)
    return {"source": FDIC_API, "rows": len(rows), "sha256": sha256(path)}


def fetch_sba_report():
    target = RAW / "sba_recovery"
    target.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(SBA_REPORT, timeout=180) as response:
        blob = response.read()
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        pdfs = [name for name in archive.namelist() if name.lower().endswith(".pdf")]
        if len(pdfs) != SBA_REPORT_PDFS:
            raise RuntimeError(f"expected {SBA_REPORT_PDFS} PDFs, found {len(pdfs)}")
        for name in pdfs:
            (target / Path(name).name).write_bytes(archive.read(name))
    archive_path = target / Path(SBA_REPORT).name
    archive_path.write_bytes(blob)
    print(f"{archive_path.name}: {len(pdfs)} PDFs, {len(blob) / 1e6:.1f} MB", flush=True)
    return {"source": SBA_REPORT, "pdfs": len(pdfs), "sha256": sha256(archive_path)}


def count_csv_rows(path):
    with path.open(newline="", encoding="latin-1") as handle:
        return sum(1 for _ in csv.reader(handle)) - 1  # quoted fields can hold newlines


def fetch_sba_file(name):
    target = RAW / "sba" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    url = f"{SBA_FOIA}/{name}"
    with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=180) as head:
        expected = int(head.headers["Content-Length"])
    if not (target.exists() and target.stat().st_size == expected):
        part = target.with_name(target.name + ".part")
        with urllib.request.urlopen(url, timeout=180) as response, part.open("wb") as handle:
            while chunk := response.read(CHUNK_BYTES):
                handle.write(chunk)
        if part.stat().st_size != expected:
            raise RuntimeError(f"{name}: {part.stat().st_size:,} bytes, server says {expected:,}")
        os.replace(part, target)
    entry = {"source": url, "bytes": expected, "sha256": sha256(target)}
    if name.endswith(".csv"):
        entry["rows"] = count_csv_rows(target)
    rows = f", {entry['rows']:,} rows" if "rows" in entry else ""
    print(f"{name}: {expected / 1e6:.1f} MB{rows}", flush=True)
    return entry


def main():
    fdic = RAW / "fdic"
    manifest = {"retrieved": date.today().isoformat(), "files": {}}
    files = manifest["files"]

    for name in SBA_FOIA_FILES:
        files[name] = fetch_sba_file(name)
    files["sba_recovery"] = fetch_sba_report()

    failures = fetch_rows("failures", FAILURE_FIELDS, "FAILDATE")
    files["failures.csv"] = save_csv(fdic / "failures.csv", failures, FAILURE_FIELDS, ["ID"])

    institutions = fetch_rows("institutions", INSTITUTION_FIELDS, "CERT")
    files["institutions.csv"] = save_csv(
        fdic / "institutions.csv", institutions, INSTITUTION_FIELDS, ["CERT"]
    )

    failed = sorted(
        {
            row["CERT"]
            for row in failures
            if row["RESTYPE"] == "FAILURE" and row.get("CERT") and int(row["FAILYR"]) >= FIRST_YEAR
        }
    )
    prefailure = []
    for start in range(0, len(failed), CERTS_PER_BATCH):
        batch = " OR ".join(str(cert) for cert in failed[start : start + CERTS_PER_BATCH])
        prefailure += fetch_rows("financials", FINANCIAL_FIELDS, "CERT", f"CERT:({batch})")
        done = min(start + CERTS_PER_BATCH, len(failed))
        print(f"  prefailure: {done:,} of {len(failed):,} banks", flush=True)
    files["financials_prefailure.csv"] = save_csv(
        fdic / "financials_prefailure.csv", prefailure, FINANCIAL_FIELDS, ["CERT", "REPDTE"]
    )

    yearend = []
    for year in range(FIRST_YEAR, LAST_YEAR + 1):
        yearend += fetch_rows("financials", FINANCIAL_FIELDS, "CERT", f"REPDTE:{year}1231")
        print(f"  yearend: {year}, {len(yearend):,} rows so far", flush=True)
    files["financials_yearend.csv"] = save_csv(
        fdic / "financials_yearend.csv", yearend, FINANCIAL_FIELDS, ["CERT", "REPDTE"]
    )

    (RAW / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print("manifest.json written", flush=True)


if __name__ == "__main__":
    main()
