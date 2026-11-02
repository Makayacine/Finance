"""Fetch the Latvian company register, annual accounts and insolvency register into ``data/raw/``.

Runs on the host, once, before anything else. The Glue jobs only ever read folders, so nothing
past this script needs a network. Standard library only, so it runs from a clean clone with
nothing installed.

    data/raw/financial_statements.csv     one row per filed annual report: who, which year, when
    data/raw/balance_sheets.csv           one balance sheet per statement
    data/raw/income_statements.csv        one income statement per statement
    data/raw/cash_flow_statements.csv     one cash-flow statement per statement, where filed
    data/raw/insolvency_legal_person_proceeding.csv   every insolvency and legal-protection case
    data/raw/register.csv                 every entity ever registered, terminated ones included
    data/raw/insolvency_proceeding_kolonnu_apzimejumi.xlsx   the insolvency file's code sheets
    data/raw/manifest.json                source, retrieval time, bytes, rows, sha256

All of it is published by the Register of Enterprises (Uznemumu registrs, organisation ``ur``) on
data.gov.lv under CC0-1.0 and refreshed every night. The download URLs are not written here:
each one is resolved from the dataset's CKAN record at run time, because a resource can be
re-uploaded under a new id while the dataset name stays put.

Each file is streamed to a ``.part`` file and renamed into place only once its byte count matches
the server's Content-Length, so an interrupted run leaves no half file that looks whole. The
manifest records the byte count, the data row count and the sha256 of every file, and the
CKAN ``last_modified`` stamp it was cut from; ``--check`` re-hashes what is on disk against it.

The register and the insolvency file carry people's names: individual merchants in the register,
and the debtor identity columns in the insolvency file. Both stay in ``data/raw/``, which is
gitignored. Every reader downstream drops them before a row is stored anywhere.
"""

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

CKAN = "https://data.gov.lv/dati/api/3/action/package_show?id={name}"
# dataset name -> the CSV files to take from it, by the file name at the end of the URL
DATASETS = {
    "gada-parskatu-finansu-dati": (
        "financial_statements.csv",
        "balance_sheets.csv",
        "income_statements.csv",
        "cash_flow_statements.csv",
    ),
    "maksatnespejas-procesi": ("insolvency_legal_person_proceeding.csv",),
    "uz": ("register.csv",),
}
# The code sheets are not a CKAN resource. https only: the http address times out.
GLOSSARY = "https://dati.ur.gov.lv/insolvency/insolvency_proceeding_kolonnu_apzimejumi.xlsx"
CHUNK_BYTES = 1 << 20

RAW = Path(__file__).resolve().parents[1] / "data" / "raw"


def open_url(url, method="GET"):
    for attempt in range(5):
        try:
            return urllib.request.urlopen(urllib.request.Request(url, method=method), timeout=300)
        except urllib.error.HTTPError as error:
            if attempt == 4 or (error.code < 500 and error.code != 429):
                raise
        except OSError:
            if attempt == 4:
                raise
        time.sleep(2**attempt)


def resolve(dataset, wanted):
    with open_url(CKAN.format(name=dataset)) as response:
        record = json.load(response)["result"]
    found = {}
    for resource in record["resources"]:
        name = resource["url"].rsplit("/", 1)[-1]
        if name in wanted:
            found[name] = {
                "url": resource["url"],
                "dataset": dataset,
                "license": record.get("license_id"),
                "last_modified": resource.get("last_modified"),
            }
    missing = set(wanted) - set(found)
    if missing:
        raise RuntimeError(f"{dataset}: no resource named {sorted(missing)}")
    return found


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def count_rows(path):
    """Data rows: lines after the header. These files quote no field across a line break."""
    with path.open("rb") as handle:
        lines = sum(chunk.count(b"\n") for chunk in iter(lambda: handle.read(CHUNK_BYTES), b""))
    return lines - 1


def download(url, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    with open_url(url) as response, part.open("wb") as handle:
        expected = response.headers.get("Content-Length")
        while chunk := response.read(CHUNK_BYTES):
            handle.write(chunk)
    got = part.stat().st_size
    if expected is None:
        raise RuntimeError(f"{target.name}: the server sent no Content-Length to check against")
    if got != int(expected):
        raise RuntimeError(f"{target.name}: {got:,} bytes, server says {int(expected):,}")
    os.replace(part, target)
    return got


def fetch():
    manifest = {"retrieved": datetime.now(timezone.utc).isoformat(timespec="seconds"), "files": {}}
    sources = {}
    for dataset, wanted in DATASETS.items():
        sources.update(resolve(dataset, wanted))
    sources[GLOSSARY.rsplit("/", 1)[-1]] = {"url": GLOSSARY, "dataset": "maksatnespejas-procesi"}

    for name, source in sources.items():
        target = RAW / name
        started = time.monotonic()
        size = download(source["url"], target)
        entry = {**source, "bytes": size, "sha256": sha256(target)}
        if name.endswith(".csv"):
            entry["rows"] = count_rows(target)
        manifest["files"][name] = entry
        rows = f", {entry['rows']:,} rows" if "rows" in entry else ""
        print(f"{name}: {size / 1e6:.1f} MB{rows} in {time.monotonic() - started:.0f} s", flush=True)

    (RAW / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print("manifest.json written", flush=True)


def check():
    manifest = json.loads((RAW / "manifest.json").read_text(encoding="utf-8"))
    bad = 0
    for name, entry in manifest["files"].items():
        path = RAW / name
        if not path.exists():
            print(f"{name}: missing")
            bad += 1
        elif path.stat().st_size != entry["bytes"] or sha256(path) != entry["sha256"]:
            print(f"{name}: differs from the manifest")
            bad += 1
        else:
            print(f"{name}: ok")
    return bad


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="re-hash data/raw/ against manifest.json and download nothing")
    args = parser.parse_args()
    if args.check:
        sys.exit(1 if check() else 0)
    fetch()


if __name__ == "__main__":
    main()
