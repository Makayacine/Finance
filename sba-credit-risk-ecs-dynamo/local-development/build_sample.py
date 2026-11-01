"""Cut the committed sample from ``data/raw/``: small, stratified, and without borrower identity.

    data/raw/sba/FOIA_7a_*.csv      ->  data/sample/sba/FOIA_7a_*.csv     0.6% of every
                                        (fiscal year, loan status) cell, at least 3 rows a cell
    data/raw/fdic/*.csv             ->  data/sample/fdic/*.csv            35 failed banks per
                                        failure era, every FDIC table restricted to them and to
                                        400 banks that never failed
                                    ->  data/sample/fdic/SOURCE.md        provenance

The sample exists so the project, its containers and its tests run straight after a clone. It is
not a small copy of the result: every figure in the README is from the full files.

**Stratified, not the head of the file.** The FOIA files are sorted by approval date, so their
first rows are FY2000 loans and almost no charge-offs from the crisis vintages. Every
(fiscal year, status) cell contributes, the rare ones at a floor of three rows, so the sample
holds CANCLD and COMMIT loans to be dropped, EXEMPT loans to be censored and every vintage the
scorecard is fitted on. The FDIC sample keeps every table of a chosen bank whole, because a bank
with half its filings would label wrongly, and it always includes the three 2023 failures the
loss chapter flags.

**The borrower columns are never read.** ``BorrName``, ``BorrStreet``, ``BorrCity`` and
``BorrZip`` are left out by ``usecols`` before a single row is in memory, so they cannot reach
the sample by any path. The SBA sample therefore has 38 of the 42 published columns, which the
validator accepts: those four are ``never_read``, not required.

**The sample must pass the validator.** A column that is never blank in 3,000 sampled rows
infers ``int64`` where the full file's blank makes it ``float64``; ``config.py``'s one widening
covers that. A text column that happened to be blank in every sampled row would infer
``float64`` and fail, which is why the script ends by running the validator over its own
output and refuses to finish if any file fails.

    python local-development/build_sample.py
"""

import json
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from credit_risk import fdic  # noqa: E402
from credit_risk.config import BORROWER_COLUMNS, ENTITIES  # noqa: E402
from credit_risk.validation import validate_file  # noqa: E402

RAW = PROJECT / "data" / "raw"
SAMPLE = PROJECT / "data" / "sample"
SEED = 20261101
SBA_FRACTION, SBA_FLOOR = 0.006, 3
FAILED_PER_ERA, SURVIVORS = 35, 400


def sample_sba():
    rng = np.random.default_rng(SEED)
    for path in sorted((RAW / "sba").glob("FOIA_7a_*.csv")):
        header = pd.read_csv(path, nrows=0).columns
        columns = [c for c in header if c not in BORROWER_COLUMNS]
        frame = pd.read_csv(path, usecols=columns, dtype=str, keep_default_na=False)
        chosen = []
        for _, idx in frame.groupby(["ApprovalFY", "LoanStatus"]).indices.items():
            take = min(len(idx), max(SBA_FLOOR, int(round(SBA_FRACTION * len(idx)))))
            chosen += list(rng.choice(idx, take, replace=False))
        out = frame.iloc[sorted(chosen)][columns]
        target = SAMPLE / "sba" / path.name
        target.parent.mkdir(parents=True, exist_ok=True)
        out.to_csv(target, index=False)
        print(f"{target.relative_to(PROJECT)}: {len(out):,} of {len(frame):,} rows")


def sample_fdic():
    rng = np.random.default_rng(SEED)
    failures = pd.read_csv(RAW / "fdic" / "failures.csv")
    yearend = pd.read_csv(RAW / "fdic" / "financials_yearend.csv")
    prefailure = pd.read_csv(RAW / "fdic" / "financials_prefailure.csv")
    institutions = pd.read_csv(RAW / "fdic" / "institutions.csv")

    events, assisted, _ = fdic.failure_events(failures)
    events = events[events.FAILYR >= 1984].copy()
    events["era"] = events.FAILYR.map(fdic.era_of)
    failed = set(fdic.SYSTEMIC_2023)
    for _, part in events.groupby("era"):
        certs = part.CERT.values
        failed |= set(rng.choice(certs, min(FAILED_PER_ERA, len(certs)), replace=False))
    failed |= set(rng.choice(assisted.CERT.values, 10, replace=False))
    never = np.setdiff1d(yearend.CERT.unique(), np.concatenate([events.CERT, assisted.CERT]))
    keep = failed | set(rng.choice(never, SURVIVORS, replace=False))

    tables = {
        "failures.csv": failures[failures.CERT.isin(keep)
                                 | (failures.COMMENTS == fdic.FSLIC_DUPLICATE_COMMENT)],
        "financials_yearend.csv": yearend[yearend.CERT.isin(keep)],
        "financials_prefailure.csv": prefailure[prefailure.CERT.isin(keep)],
        "institutions.csv": institutions[institutions.CERT.isin(keep)],
    }
    for name, table in tables.items():
        target = SAMPLE / "fdic" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        table.to_csv(target, index=False)
        print(f"{target.relative_to(PROJECT)}: {len(table):,} rows")
    manifest = RAW / "manifest.json"
    retrieved = (json.loads(manifest.read_text(encoding="utf-8"))["retrieved"]
                 if manifest.exists() else str(date.today()))
    (SAMPLE / "fdic" / "SOURCE.md").write_text(
        f"Source: FDIC BankFind Suite API, retrieved {retrieved}\n\n"
        f"{len(keep):,} banks: {len(failed):,} that failed or were assisted, stratified by "
        f"failure era and always including the three 2023 failures, and {SURVIVORS} that never "
        f"failed. Every table is cut to those CERTs whole, plus the 42 FSLIC-RTC duplicate "
        f"failure rows. Dollar fields are in thousands, as the API publishes them.\n",
        encoding="utf-8")


def check():
    for entity in ENTITIES:
        root = PROJECT / "data" if entity.shared_source else SAMPLE
        files = sorted(root.glob(entity.source_glob))
        assert len(files) == entity.expected_files, (entity.name, files)
        for path in files:
            ok, reason, rows = validate_file(path, entity)
            if not ok:
                raise SystemExit(f"{path.name} fails its own validator: {reason}")
            print(f"valid: {entity.name}/{path.name}, {rows:,} rows")


if __name__ == "__main__":
    sample_sba()
    sample_fdic()
    check()
