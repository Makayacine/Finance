"""MERGE every batch waiting in raw_zone/ into one Delta table, oldest first, then archive it.

AWS Glue 5.0 entrypoint (Spark 3.5.4, Delta Lake 3.3.0 native through ``--datalake-formats
delta``). The reference lab's job, for three tables instead of three supermarket ones::

    --table companies     raw_zone/register/                     -> lakehouse-dwh/companies
                          key regcode; restated when the company is terminated
    --table proceedings   raw_zone/insolvency_legal_person_proceeding/ -> lakehouse-dwh/proceedings
                          key proceeding_id; arrives open, restated when it ends
    --table statements    raw_zone/financial_statements/ + balance_sheets/ + income_statements/
                          -> lakehouse-dwh/statements, partitioned by fiscal_year
                          key (regcode, fiscal_year); restated by a refiling or a later period

Each batch file is ``<source>_<YYYY-MM-DD>.csv``. Batches are merged in date order, one Delta
commit each, carrying the batch date and the batch's counts in the commit's ``userMetadata``; the
files are moved to ``archived/<source>/`` only once their commit is in. A failure stops the job
with the failing batch and every later one still in raw_zone/, so a re-run carries on from there.

Local run, after ``local-development/stage_batches.py --local``::

    python glue-jobs/glue-delta-merge.py --local --table companies
    python glue-jobs/glue-delta-merge.py --local --table proceedings
    python glue-jobs/glue-delta-merge.py --local --table statements

The curation lives here and nowhere else: group accounts are dropped, lats are converted at the
fixed rate, statements rounded to thousands are scaled, and each statement gets the availability
date the replay dated it by. ``company_credit.config`` holds every one of those rules.
"""

import argparse
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pyspark.sql import functions as F  # noqa: E402

import lakehouse as lh  # noqa: E402
from company_credit import config  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("glue_delta_merge")

# key, ordering (newest last), the columns whose change is a restatement, partition column.
# An end state outranks the state before it whatever its batch date; the batch date breaks ties.
NEVER = "DATE'0001-01-01'"
TABLES = {
    "companies": dict(
        key=["regcode"], order=[f"coalesce({{}}.terminated, {NEVER})", "batch_date"],
        values=["type", "registered", "terminated", "closed"], partition=None),
    "proceedings": dict(
        key=["proceeding_id"],
        order=[f"coalesce({{}}.proceeding_ended_on, {NEVER})", "batch_date"],
        values=["regcode", "proceeding_started_on", "proceeding_ended_on", "proceeding_form",
                "proceeding_type", "proceeding_resolution_type", "entry_created_on"],
        partition=None),
    "statements": dict(
        key=["regcode", "fiscal_year"], order=["year_ended_on", "created_at", "statement_id"],
        values=["statement_id"], partition="fiscal_year"),
}


def curate_companies(spark, bucket, keys, batch_date):
    frame = lh.read_batch(spark, bucket, "register", keys["register"])
    kept = frame.filter(F.col("type").isin(*config.COMPANY_TYPES))
    return kept.withColumn("batch_date", F.lit(batch_date)), {}


def curate_proceedings(spark, bucket, keys, batch_date):
    frame = lh.read_batch(spark, bucket, "insolvency_legal_person_proceeding",
                          keys["insolvency_legal_person_proceeding"])
    frame = frame.withColumnRenamed("debtor_registration_number", "regcode")
    return frame.withColumn("batch_date", F.lit(batch_date)), {}


def curate_statements(spark, bucket, keys, batch_date):
    statements = lh.read_batch(spark, bucket, "financial_statements", keys["financial_statements"])
    balances = lh.read_batch(spark, bucket, "balance_sheets", keys["balance_sheets"])
    incomes = lh.read_batch(spark, bucket, "income_statements", keys["income_statements"])

    # source_type is empty on every pre-2016 schema, and isin() on a null is null, which a
    # filter treats as false: without the coalesce every 2008-2015 statement would be dropped.
    group = (F.coalesce(F.col("source_type").isin(*config.CONSOLIDATED_SOURCE_TYPES), F.lit(False))
             | F.coalesce(F.col("source_schema").isin(*config.CONSOLIDATED_SCHEMAS), F.lit(False)))
    consolidated = statements.filter(group).count()
    statements = statements.filter(~group)

    created = F.to_timestamp("created_at")
    assumed = F.add_months("year_ended_on", config.EMBARGO_MONTHS)
    bulk = created < F.lit(config.BULK_LOAD_END.isoformat()).cast("timestamp")
    frame = (
        statements.withColumnRenamed("id", "statement_id")
        .withColumnRenamed("legal_entity_registration_number", "regcode")
        .withColumnRenamed("year", "fiscal_year")
        .withColumn("created_at", created)
        .withColumn("bulk_dated", bulk)
        .withColumn("available_on", F.when(bulk, F.least(F.to_date(created), assumed))
                    .otherwise(F.to_date(created)))
        .join(balances, "statement_id", "left")
        .join(incomes, "statement_id", "left")
    )
    rate = (F.when(F.col("currency") == "LVL", F.lit(1 / config.LVL_PER_EUR)).otherwise(1.0)
            * F.when(F.col("rounded_to_nearest") == "THOUSANDS",
                     config.SCALE["THOUSANDS"]).otherwise(1.0))
    for column in config.AMOUNTS:
        frame = frame.withColumn(column, (F.col(column) * rate).cast("double"))
    frame = frame.withColumnRenamed("currency", "reported_currency")
    return frame.withColumn("batch_date", F.lit(batch_date)), {"consolidated_dropped": consolidated}


CURATE = {"companies": curate_companies, "proceedings": curate_proceedings,
          "statements": curate_statements}


def batches_for(bucket, table):
    """{batch date: {source: key}}; a statement batch must hold all three of its files."""
    sources = config.SOURCES[table]
    pending = {source: lh.pending_batches(bucket, source) for source in sources}
    days = sorted(set().union(*pending.values()))
    batches = {}
    for day in days:
        missing = [s for s in sources if day not in pending[s]]
        if missing:
            raise FileNotFoundError(
                f"batch {day}: {', '.join(f'{m}_{day}.csv' for m in missing)} missing from "
                f"raw_zone; the batch is left where it is")
        batches[day] = {source: pending[source][day] for source in sources}
    return batches


def run(spark, bucket, table, limit=None):
    spec = TABLES[table]
    path = lh.table_path(bucket, table)
    batches = batches_for(bucket, table)
    if not batches:
        LOG.info("%s: nothing in raw_zone", table)
        return []
    log = []
    for day in sorted(batches)[:limit]:
        started = time.monotonic()
        keys = batches[day]
        curated, counts = CURATE[table](spark, bucket, keys, day)
        # Cached, so the counts and the MERGE share one read of the batch's files.
        curated = curated.cache()
        frame, duplicates = lh.keep_winner(curated, spec["key"], spec["order"])
        rows = frame.count()
        note = lh.commit_note(day, source_rows=rows, duplicates_dropped=duplicates, **counts)
        version, metrics = lh.merge_batch(
            spark, path, frame, spec["key"], spec["order"], spec["values"], note,
            partition_by=spec["partition"])
        for source, key in keys.items():
            # Raises on failure: a merged batch must never sit in raw_zone looking unmerged
            # without the run saying so. Re-running it is safe; see lakehouse.merge_batch.
            bucket.move(key, f"{lh.ARCHIVED}/{source}/{key.rsplit('/', 1)[-1]}")
        frame.unpersist()
        curated.unpersist()
        entry = summarise(table, day, version, rows, duplicates, metrics, counts)
        entry["seconds"] = round(time.monotonic() - started, 1)
        log.append(entry)
        LOG.info("%s %s v%d: %s", table, day, version, json.dumps(entry))
    return log


def summarise(table, day, version, rows, duplicates, metrics, counts):
    inserted = int(metrics.get("numTargetRowsInserted", metrics.get("numOutputRows", rows)))
    updated = int(metrics.get("numTargetRowsUpdated", 0))
    return {
        "table": table, "batch_date": day.isoformat(), "version": version, "rows": rows,
        "inserted": inserted, "restated": updated, "unchanged": rows - inserted - updated,
        "restatement_share": round(updated / rows, 6) if rows else 0.0,
        "duplicates_dropped": duplicates,
        "files_removed": int(metrics.get("numTargetFilesRemoved", 0)), **counts,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--table", required=True, choices=sorted(TABLES))
    parser.add_argument("--local", action="store_true",
                        help="a local folder stands in for the bucket; master local[*]")
    parser.add_argument("--root", default=str(lh.PROJECT / "_localrun" / "bucket"))
    parser.add_argument("--bucket", help="the S3 bucket holding raw_zone/, lakehouse-dwh/ "
                                         "and archived/ (on Glue)")
    parser.add_argument("--limit", type=int, help="merge at most this many batches, oldest first")
    parser.add_argument("--log-json", help="also write the per-batch log to this local file")
    # parse_known_args: Glue appends --JOB_NAME and its own arguments to every run.
    args, _ = parser.parse_known_args()
    if not args.local and not args.bucket:
        parser.error("--bucket is required unless --local")

    spark = lh.build_session(f"latvia-delta-merge-{args.table}", args.local)
    try:
        log = run(spark, lh.open_bucket(args), args.table, args.limit)
    finally:
        spark.stop()
    if args.log_json:
        Path(args.log_json).write_text(json.dumps(log, indent=1), encoding="utf-8")
    restated = sum(e["restated"] for e in log)
    rows = sum(e["rows"] for e in log)
    LOG.info("%s: %d batches, %d rows, %d restated (%.2f%%)", args.table, len(log), rows,
             restated, 100 * restated / rows if rows else 0.0)


if __name__ == "__main__":
    main()
