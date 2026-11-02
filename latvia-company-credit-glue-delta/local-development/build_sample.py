"""Cut the committed sample in ``data/sample/`` from the full files in ``data/raw/``.

    python local-development/build_sample.py

The sample is stratified by company, not the head of a file, and every table is cut to the
sampled companies whole, so a sampled company keeps all of its statements, proceedings and its
register row:

* up to 70 companies whose first default started in each year 2008-2026, so every observation
  year has defaults to label;
* 60 companies whose only applications were dismissed, so the dismissal rule has cases;
* 40 companies that filed group accounts and 40 with two statements for one fiscal year, so the
  curation and the in-batch winner have something to do;
* 3,600 companies with no default, drawn evenly from the live and the terminated.

Only the allow-listed columns are written -- the register's regcode, type and dates, and the
insolvency file without its debtor identity columns -- and only companies, so the sample holds no
name, no address and no individual merchant. The files keep the source's ``;`` format and are
gzipped; the stage script reads them the same way it reads the full files. CC0-1.0, like the
source. The draw is seeded, so re-running it on the same raw files gives the same sample.
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "local-development"))
sys.path.insert(0, str(PROJECT))

from company_credit import config  # noqa: E402
from stage_batches import FILES, read_companies, read_source  # noqa: E402

RAW, SAMPLE = PROJECT / "data" / "raw", PROJECT / "data" / "sample"
SEED = 20261102
PER_DEFAULT_YEAR, DISMISSED_ONLY, GROUP, TWO_A_YEAR, CLEAN = 70, 60, 40, 40, 3600


def draw(rng, pool, n):
    pool = sorted(pool)
    return set(rng.choice(pool, size=min(n, len(pool)), replace=False)) if pool else set()


def main():
    rng = np.random.default_rng(SEED)
    register = read_companies(RAW)
    companies = set(register.regcode)
    proceedings = read_source(RAW, "insolvency_legal_person_proceeding")
    proceedings = proceedings[proceedings.debtor_registration_number.isin(companies)]
    statements = read_source(RAW, "financial_statements")
    statements = statements[statements.legal_entity_registration_number.isin(companies)]

    real = proceedings[~proceedings.proceeding_resolution_type.isin(config.DISMISSED)]
    first = real.groupby("debtor_registration_number").proceeding_started_on.min().str[:4]
    chosen = set()
    for year, regcodes in first.groupby(first):
        chosen |= draw(rng, regcodes.index, PER_DEFAULT_YEAR)
    defaulted = set(first.index)
    dismissed_only = set(proceedings.debtor_registration_number) - defaulted
    chosen |= draw(rng, dismissed_only, DISMISSED_ONLY)

    group = statements.source_type.isin(config.CONSOLIDATED_SOURCE_TYPES) | \
        statements.source_schema.isin(config.CONSOLIDATED_SCHEMAS)
    chosen |= draw(rng, set(statements[group].legal_entity_registration_number), GROUP)
    own = statements[~group]
    twice = own.groupby(["legal_entity_registration_number", "year"]).size()
    chosen |= draw(rng, set(twice[twice > 1].index.get_level_values(0)), TWO_A_YEAR)

    clean = register[~register.regcode.isin(defaulted | dismissed_only)]
    live = clean[clean.terminated.isna()].regcode
    gone = clean[clean.terminated.notna()].regcode
    chosen |= draw(rng, live, CLEAN // 2) | draw(rng, gone, CLEAN // 2)

    SAMPLE.mkdir(parents=True, exist_ok=True)
    cut = {
        "register": register[register.regcode.isin(chosen)],
        "insolvency_legal_person_proceeding":
            proceedings[proceedings.debtor_registration_number.isin(chosen)],
        "financial_statements": statements[statements.legal_entity_registration_number.isin(chosen)],
    }
    ids = set(cut["financial_statements"].id)
    for source in ("balance_sheets", "income_statements"):
        frame = read_source(RAW, source)
        cut[source] = frame[frame.statement_id.isin(ids)]
    for source, frame in cut.items():
        path = SAMPLE / (FILES[source] + ".gz")
        frame.to_csv(path, sep=";", index=False, compression={"method": "gzip", "mtime": 0})
        print(f"{path.name}: {len(frame):,} rows, {path.stat().st_size / 1e6:.2f} MB")
    print(f"{len(chosen):,} companies, {len(defaulted & chosen):,} with a default")


if __name__ == "__main__":
    main()
