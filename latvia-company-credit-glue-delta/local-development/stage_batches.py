"""Stage the source files into ``raw_zone/`` as dated batches, the way a nightly extract lands.

    python local-development/stage_batches.py --local --data sample --clean
    python local-development/stage_batches.py --local --data raw --clean
    python local-development/stage_batches.py --local --data raw --clean --through 2012-12-31
    python local-development/stage_batches.py --bucket <bucket> --data raw

Reads ``data/<data>/`` and writes, for every batch date, one file per source::

    raw_zone/register/register_<date>.csv
    raw_zone/insolvency_legal_person_proceeding/insolvency_legal_person_proceeding_<date>.csv
    raw_zone/financial_statements/financial_statements_<date>.csv
    raw_zone/balance_sheets/balance_sheets_<date>.csv
    raw_zone/income_statements/income_statements_<date>.csv

in the source's own format -- ``;``-delimited, a header row, the source's column names and
values -- so the Glue job reads a batch exactly as it would read the published file. ``--local``
maps the bucket onto ``_localrun/bucket/``; ``--bucket`` uploads to S3 with boto3.

**Who is read.** Every source is read through the column lists in ``company_credit.config``, so
the register's name and address and the insolvency file's debtor identity columns are never
parsed. The register's type is read first and only companies are kept; the other two sources are
cut to those companies' registration numbers before a batch is written. No individual merchant,
farm or person reaches ``raw_zone/``.

``--cadence year`` cuts one batch a year on 30 September instead of one a quarter. Every
observation date is a 1 October, so the model reads the same rows either way.
``--through`` stops at a batch date, so the chain can be run forward one batch at a time.
``--summary`` prints each batch's rows and restatement share per source and writes nothing.
"""

import argparse
import shutil
import sys
from pathlib import Path

import pandas as pd

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from company_credit import config, replay  # noqa: E402

FILES = {
    "register": "register.csv",
    "insolvency_legal_person_proceeding": "insolvency_legal_person_proceeding.csv",
    "financial_statements": "financial_statements.csv",
    "balance_sheets": "balance_sheets.csv",
    "income_statements": "income_statements.csv",
}


def source_path(folder, source):
    plain = folder / FILES[source]
    return plain if plain.exists() else plain.with_name(plain.name + ".gz")


def read_source(folder, source):
    """Only the allow-listed columns, every value as the text the file holds."""
    return pd.read_csv(
        source_path(folder, source), sep=";", usecols=list(config.SOURCE_COLUMNS[source]),
        dtype=str, keep_default_na=False, na_values=[""], encoding="utf-8-sig",
    )[list(config.SOURCE_COLUMNS[source])]


def read_companies(folder):
    register = read_source(folder, "register")
    return register[register.type.isin(config.COMPANY_TYPES)].reset_index(drop=True)


def build(folder, cadence="quarter"):
    register = read_companies(folder)
    companies = set(register.regcode)
    proceedings = read_source(folder, "insolvency_legal_person_proceeding")
    proceedings = proceedings[proceedings.debtor_registration_number.isin(companies)]
    statements = read_source(folder, "financial_statements")
    statements = statements[statements.legal_entity_registration_number.isin(companies)]
    ids = set(statements.id)
    balances = read_source(folder, "balance_sheets")
    balances = balances[balances.statement_id.isin(ids)]
    incomes = read_source(folder, "income_statements")
    incomes = incomes[incomes.statement_id.isin(ids)]

    last = replay.last_batch_date(register, proceedings, statements)
    fs, bs, inc = replay.statement_batches(statements, balances, incomes, last, cadence)
    return {
        "register": replay.register_batches(register, last, cadence),
        "insolvency_legal_person_proceeding": replay.proceeding_batches(proceedings, last, cadence),
        "financial_statements": fs,
        "balance_sheets": bs,
        "income_statements": inc,
    }


class LocalBucket:
    def __init__(self, root):
        self.root = Path(root)

    def clean(self):
        for zone in ("raw_zone", "lakehouse-dwh", "archived"):
            shutil.rmtree(self.root / zone, ignore_errors=True)

    def put(self, key, frame):
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        part = path.with_name(path.name + ".part")
        frame.to_csv(part, sep=";", index=False, encoding="utf-8")
        part.replace(path)  # a half-written batch never carries a batch file's name


class S3Bucket:
    def __init__(self, bucket):
        import boto3  # only the S3 path needs it

        self.client = boto3.client("s3")
        self.bucket = bucket

    def clean(self):
        raise SystemExit("--clean is local only; empty an S3 prefix deliberately, not by flag")

    def put(self, key, frame):
        body = frame.to_csv(sep=";", index=False).encode("utf-8")
        self.client.put_object(Bucket=self.bucket, Key=key, Body=body,
                               ContentType="text/csv; charset=utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data", choices=("sample", "raw"), default="sample")
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--local", action="store_true", help="write to _localrun/bucket/")
    target.add_argument("--bucket", help="S3 bucket to upload the batches to")
    parser.add_argument("--root", default=str(PROJECT / "_localrun" / "bucket"))
    parser.add_argument("--cadence", choices=sorted(replay.CADENCES), default="quarter",
                        help="quarterly batches, or one a year cut on 30 September: the same "
                             "as-of views on every observation date in a quarter of the commits")
    parser.add_argument("--through", help="stage batches dated on or before this day only")
    parser.add_argument("--after", help="stage batches dated after this day only")
    parser.add_argument("--clean", action="store_true",
                        help="empty raw_zone/, lakehouse-dwh/ and archived/ first (local only)")
    parser.add_argument("--summary", action="store_true", help="print the batches, write nothing")
    args = parser.parse_args()

    batches = build(PROJECT / "data" / args.data, args.cadence)
    dates = sorted(set().union(*(set(frame.batch_date) for frame in batches.values())))
    if args.through:
        dates = [d for d in dates if d <= pd.Timestamp(args.through)]
    if args.after:
        dates = [d for d in dates if d > pd.Timestamp(args.after)]

    if args.summary:
        keys = {"register": ["regcode"], "insolvency_legal_person_proceeding": ["proceeding_id"],
                "financial_statements": ["legal_entity_registration_number", "year"]}
        for source, key in keys.items():
            table = replay.restatement_share(batches[source], key)
            print(f"{source}: {len(table)} batches, {table.rows.sum():,} rows, "
                  f"{table.restated.sum():,} restated ({table.restated.sum() / table.rows.sum():.2%})")
        return

    bucket = S3Bucket(args.bucket) if args.bucket else LocalBucket(args.root)
    if args.clean:
        bucket.clean()
    written = 0
    statement_days = set(batches["financial_statements"].batch_date)
    for day in dates:
        stamp = pd.Timestamp(day).date().isoformat()
        for source, frame in batches.items():
            rows = frame[frame.batch_date == day].drop(columns="batch_date")
            # A statement batch is its three files together, so all three are written, a header
            # alone where a quarter has no balance sheet or income statement: the job refuses a
            # batch with a file missing rather than merge statements with no figures.
            whole = source in config.SOURCES["statements"] and day in statement_days
            if len(rows) or whole:
                bucket.put(f"raw_zone/{source}/{source}_{stamp}.csv", rows)
                written += 1
    print(f"{len(dates)} batches, {dates[0].date()} to {dates[-1].date()}: {written} files "
          f"staged from data/{args.data}/")


if __name__ == "__main__":
    main()
