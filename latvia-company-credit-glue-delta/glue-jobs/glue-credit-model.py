"""Build the PD's training rows by Delta time travel, fit the scorecard, score today's companies.

AWS Glue 5.0 entrypoint. Runs after the three MERGE jobs::

    for each observation year (1 October 2009, 2010, ...):
        companies, statements, proceedings   read AS OF the batch dated 30 September
        proceedings                          read AS OF the batch dated 30 September a year on
        -> company_credit.panel.observation_rows: population, accounts, label
    company_credit.analysis.run   fit on 2009-17, calibrate on 2018-20, test on 2021-25
    -> results/model/*.csv        every table the README quotes
    -> results/pd_rows/           the company-year rows, Parquet (and the test years re-dated
                                  the bulk way, results/pd_rows_bulk_dated/)
    -> lakehouse-dwh/scored_companies   MERGE on (regcode, observation_date): a new date adds
                                        rows; a re-run on the same date replaces changed scores

**Why time travel and not today's tables with date filters.** A filter on today's table can drop
rows dated after the observation date, but it cannot bring back a value that has since been
overwritten: a proceeding that was open on the observation date and has since been resolved
shows today's resolution, and a terminated company shows its termination. Reading the table as
it stood after the batch of the eve of the observation date gives exactly what was known then.
Each version is found by the batch date in its commit, never by ``timestampAsOf``.

The model is numpy and pandas only (company_credit.logit), both shipped with Glue 5.0, so the
job needs no ``--additional-python-modules``.

    python glue-jobs/glue-credit-model.py --local
"""

import argparse
import io
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd  # noqa: E402
from pyspark.sql import functions as F  # noqa: E402

import lakehouse as lh  # noqa: E402
from company_credit import analysis, config, panel  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("glue_credit_model")

COMPANY_COLUMNS = ["regcode", "type", "registered", "terminated"]
PROCEEDING_COLUMNS = ["proceeding_id", "regcode", "proceeding_started_on", "proceeding_ended_on",
                      "proceeding_form", "proceeding_resolution_type", "entry_created_on"]
STATEMENT_COLUMNS = (["regcode", "statement_id", "fiscal_year", "year_started_on", "year_ended_on",
                      "created_at", "available_on", "bulk_dated", "employees"]
                     + list(config.AMOUNTS))
SCORED_COLUMNS = ["regcode", "observation_date", "fiscal_year", "statement_id", "accounts_lag",
                  "score", "pd_ttc", "pd_validation", "exposure_proxy", "model_trained",
                  "scored_at"]
# What a re-run on the same observation date may change. scored_at is not among them: it changes
# on every run, and a row whose score did not move is left as it was, with the time it was set.
SCORE_VALUES = ["fiscal_year", "statement_id", "accounts_lag", "score", "pd_ttc", "pd_validation",
                "exposure_proxy", "model_trained"]


class Lake:
    """The three tables, their batch versions, and as-of reads into pandas."""

    def __init__(self, spark, bucket):
        self.spark = spark
        self.paths = {t: lh.table_path(bucket, t) for t in ("companies", "proceedings",
                                                             "statements")}
        self.versions = {t: lh.batch_versions(spark, p) for t, p in self.paths.items()}

    def as_of(self, table, day, columns):
        frame = lh.read_as_of(self.spark, self.paths[table], self.versions[table], day)
        return None if frame is None else frame.select(*columns)

    def companies(self, day):
        return self.as_of("companies", day, COMPANY_COLUMNS).toPandas()

    def proceedings(self, day):
        return self.as_of("proceedings", day, PROCEEDING_COLUMNS).toPandas()

    def statements(self, day, obs, redate=False):
        frame = (self.spark.read.format("delta").load(self.paths["statements"]) if redate
                 else self.as_of("statements", day, STATEMENT_COLUMNS))
        if frame is None:
            return pd.DataFrame(columns=STATEMENT_COLUMNS)
        frame = frame.select(*STATEMENT_COLUMNS)
        if redate:
            # The sensitivity: every statement dated the bulk way, as if no filing date existed.
            frame = frame.withColumn("available_on",
                                     F.add_months("year_ended_on", config.EMBARGO_MONTHS))
        oldest = (pd.Timestamp(obs) - pd.DateOffset(months=config.MAX_ACCOUNTS_AGE_MONTHS)).date()
        frame = frame.filter((F.col("available_on") < F.lit(obs))
                             & (F.col("year_ended_on") < F.lit(obs))
                             & (F.col("year_ended_on") >= F.lit(oldest)))
        return frame.toPandas()

    def as_of_date(self):
        """The newest entry_created_on in the insolvency register: labels stop there."""
        latest = self.spark.read.format("delta").load(self.paths["proceedings"])
        return latest.agg(F.max("entry_created_on")).collect()[0][0].date()


def build_rows(lake, years, redate=False, labelled=True):
    frames = []
    for year in years:
        started = time.monotonic()
        obs = panel.observation_date(year)
        eve = panel.eve(obs)
        end_eve = panel.eve(panel.horizon_end(year))
        rows = panel.observation_rows(
            lake.companies(eve), lake.statements(eve, obs, redate=redate),
            lake.proceedings(eve), lake.proceedings(end_eve) if labelled else None,
            year, labelled=labelled)
        frames.append(rows)
        LOG.info("rows %d%s: %d live, %d covered%s in %.0f s", year, " (bulk-dated)" if redate
                 else "", len(rows), int(rows.covered.sum()),
                 f", {int(rows.y.sum())} defaults" if labelled else "", time.monotonic() - started)
    return pd.concat(frames, ignore_index=True)


def dismissal_flips(lake, rows, as_of):
    """Labels that today's insolvency register would set differently, counting only proceedings
    entered by each horizon's end: the applications undecided then and dismissed since."""
    today = lake.proceedings(as_of)
    out = []
    for year, part in rows.groupby("observation_year"):
        end = panel.horizon_end(year)
        known = today[pd.to_datetime(today.entry_created_on) < pd.Timestamp(end)]
        now = panel.labels(part.regcode, known, end).set_index("regcode").y
        then = part.set_index("regcode").y
        out.append({"observation_year": year, "defaults": int(then.sum()),
                    "dismissed_since": int(((then == 1) & (now.reindex(then.index) == 0)).sum())})
    return pd.DataFrame(out)


def batch_log(spark, bucket):
    from delta.tables import DeltaTable

    rows = []
    for table in ("companies", "proceedings", "statements"):
        history = DeltaTable.forPath(spark, lh.table_path(bucket, table)).history().collect()
        for h in history:
            if not h.userMetadata:
                continue
            note, m = json.loads(h.userMetadata), dict(h.operationMetrics or {})
            source_rows = note.get("source_rows", int(m.get("numOutputRows", 0)))
            inserted = int(m.get("numTargetRowsInserted", m.get("numOutputRows", 0)))
            restated = int(m.get("numTargetRowsUpdated", 0))
            rows.append({"table": table, "version": h.version, "batch_date": note["batch_date"],
                         "operation": h.operation, "rows": source_rows, "inserted": inserted,
                         "restated": restated, "unchanged": source_rows - inserted - restated,
                         "duplicates_dropped": note.get("duplicates_dropped", 0),
                         "consolidated_dropped": note.get("consolidated_dropped", 0)})
    log = pd.DataFrame(rows).sort_values(["table", "version"])
    log["restatement_share"] = log.restated / log.rows.where(log.rows > 0)
    return log


def write_csv(bucket, key, frame):
    buffer = io.StringIO()
    frame.to_csv(buffer, index=False)
    bucket.put_text(key, buffer.getvalue())


def merge_scores(spark, bucket, live, trained, scored_at=None):
    """MERGE the live book into scored_companies on (regcode, observation_date).

    A new observation date inserts its rows beside the earlier dates'. A re-run on a date already
    there -- after a correction to the accounts, say -- ranks after the stored rows by
    ``scored_at``, the run's own time, and so replaces every score that changed; a row whose
    score is the same is counted and left alone.
    """
    obs = panel.observation_date(int(live.observation_year.iloc[0]))
    scored_at = scored_at or datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
    out = live.assign(observation_date=obs, model_trained=f"{trained[0]}-{trained[1]}",
                      scored_at=scored_at)
    out = out[SCORED_COLUMNS].copy()
    out["fiscal_year"] = out.fiscal_year.astype("int64")
    out["statement_id"] = out.statement_id.astype("int64")
    frame = (spark.createDataFrame(out)
             .withColumn("observation_date", F.col("observation_date").cast("date"))
             .withColumn("scored_at", F.col("scored_at").cast("timestamp")))
    note = json.dumps({"batch_date": obs.isoformat(), "rows": len(out)})
    return lh.merge_batch(spark, lh.table_path(bucket, "scored_companies"), frame,
                          ["regcode", "observation_date"], ["scored_at"], SCORE_VALUES, note)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--local", action="store_true")
    parser.add_argument("--root", default=str(lh.PROJECT / "_localrun" / "bucket"))
    parser.add_argument("--bucket")
    parser.add_argument("--skip-sensitivity", action="store_true",
                        help="leave out the bulk-dated rebuild of the test years")
    args, _ = parser.parse_known_args()
    if not args.local and not args.bucket:
        parser.error("--bucket is required unless --local")

    started = time.monotonic()
    spark = lh.build_session("latvia-credit-model", args.local)
    spark.conf.set("spark.sql.execution.arrow.pyspark.enabled", "true")
    bucket = lh.open_bucket(args)
    try:
        lake = Lake(spark, bucket)
        as_of = lake.as_of_date()
        years = panel.labelled_years(as_of)
        live_year = years[-1] + 1 if panel.observation_date(years[-1] + 1) <= as_of else None
        LOG.info("as of %s: labelled years %d-%d, live year %s", as_of, years[0], years[-1],
                 live_year)
        rows = build_rows(lake, years)
        live = build_rows(lake, [live_year], labelled=False) if live_year else None
        bulk = None
        if not args.skip_sensitivity:
            bulk = build_rows(lake, range(config.TEST[0], config.TEST[1] + 1), redate=True)
        result = analysis.run(rows, live_rows=live, bulk_rows=bulk)
        result["dismissal_flips"] = dismissal_flips(lake, rows, as_of)
        result["batches"] = batch_log(spark, bucket)
        result["run"] = pd.DataFrame([{"as_of": as_of.isoformat(), "rows": len(rows),
                                       "first_year": years[0], "last_year": years[-1],
                                       "live_year": live_year}])

        for name, table in result.items():
            if isinstance(table, pd.DataFrame) and name not in ("scored", "live"):
                write_csv(bucket, f"{lh.RESULTS}/model/{name}.csv", table)
        keep = list(rows.columns)
        (spark.createDataFrame(rows[keep].assign(
            registered=pd.to_datetime(rows.registered), default_start=pd.to_datetime(rows.default_start)))
         .write.mode("overwrite").parquet(bucket.uri(f"{lh.RESULTS}/pd_rows")))
        if bulk is not None:
            (spark.createDataFrame(bulk[keep].assign(
                registered=pd.to_datetime(bulk.registered),
                default_start=pd.to_datetime(bulk.default_start)))
             .write.mode("overwrite").parquet(bucket.uri(f"{lh.RESULTS}/pd_rows_bulk_dated")))
        if live is not None:
            version, metrics = merge_scores(spark, bucket, result["live"], config.TRAIN)
            LOG.info("scored_companies v%d: %s", version, metrics)
        windows = result["windows"].set_index("window")
        LOG.info("AUC train %.3f | validate %.3f | test %.3f | %.0f s",
                 windows.auc["train"], windows.auc["validate"], windows.auc["test"],
                 time.monotonic() - started)
    finally:
        spark.stop()


if __name__ == "__main__":
    main()
