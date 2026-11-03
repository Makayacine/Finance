"""Shared by both Glue jobs: the Spark session, the bucket, the source schemas and the Delta MERGE.

The bucket has the reference lab's three zones::

    raw_zone/<source>/<source>_<batch date>.csv     what has landed and not yet been merged
    lakehouse-dwh/<table>/                          the Delta tables
    archived/<source>/<source>_<batch date>.csv     what has been merged

``--local`` maps the bucket onto a folder, so both jobs run on a laptop with no AWS account; on
Glue it is ``s3://<bucket>/``. Only ``S3Bucket`` imports boto3, and only when it is built.

WHAT CHANGED FROM THE REFERENCE, AND WHY
----------------------------------------
* **Explicit schemas, never inferSchema.** inference costs a second pass over every file, and it
  types a column from whatever one batch holds: in a quarter in which every proceeding is still
  open, ``proceeding_ended_on`` is empty and infers as a string, and the identifiers infer as
  numbers. Delta casts them back to the table's types on the MERGE, so such a batch commits, but
  a value that will not cast -- an impossible date -- then surfaces only as a failed task inside
  the MERGE's write. With the declared schema the read refuses it, naming the value. The header
  must also match the schema by name (``enforceSchema=False``), so a reordered extract is refused
  instead of read into the wrong columns, and ``FAILFAST`` raises on a malformed row instead of
  nulling it.
* **The MERGE updates only a newer, changed row.** The reference's bare ``whenMatchedUpdateAll``
  lets an older extract, delivered late, overwrite a newer one, and counts a re-delivered
  unchanged row as an update. Here the incoming row must rank after the stored one and differ
  from it in some value. The rank is the row's own state first -- a terminated company after a
  live one, a closed proceeding after an open one, a statement by period end, filing time and id
  -- and the batch date only after that, so even a stale extract under a new date cannot reopen
  a closed proceeding, and ``numTargetRowsUpdated`` is the true number of restatements.
* **One row per key per batch.** Delta refuses a MERGE in which two source rows match one target
  row. Two statements for the same company and year in one quarter are normal (a short year, a
  refiling), so each batch is first cut to its winner by the same ordering.
* **The batch date travels in the commit.** ``timestampAsOf`` is the wall-clock commit time,
  which on a replay is the same afternoon for every batch, so each commit carries its batch date
  in ``userMetadata`` and time travel looks versions up by it.
* **History is kept.** Delta cleans its log after 30 days by default, which would quietly end
  time travel to the early batches; both retention periods are set to a hundred years.
* **Archive last, loudly.** Files move to ``archived/`` only after their MERGE has committed, and
  a failed move raises. The reference logged and swallowed it, so a batch could be merged and
  left in ``raw_zone/`` with nothing saying so.
"""

import json
import os
import re
import shutil
import sys
from datetime import date
from importlib import metadata
from pathlib import Path

from pyspark.sql import SparkSession
from pyspark.sql import functions as F
from pyspark.sql import types as T

PROJECT = Path(__file__).resolve().parents[1]
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

from company_credit import config  # noqa: E402

RAW, LAKE, ARCHIVED, RESULTS = "raw_zone", "lakehouse-dwh", "archived", "results"
BATCH_FILE = re.compile(r"^(?P<source>[a-z_]+)_(?P<day>\d{4}-\d{2}-\d{2})\.csv$")
RETENTION = "interval 36500 days"


# --- session -------------------------------------------------------------------------------------

def build_session(app, local, shuffle_partitions=8, driver_memory="6g"):
    builder = (
        SparkSession.builder.appName(app)
        .config("spark.sql.extensions", "io.delta.sql.DeltaSparkSessionExtension")
        .config("spark.sql.catalog.spark_catalog",
                "org.apache.spark.sql.delta.catalog.DeltaCatalog")
        .config("spark.databricks.delta.properties.defaults.logRetentionDuration", RETENTION)
        .config("spark.databricks.delta.properties.defaults.deletedFileRetentionDuration",
                RETENTION)
        # Every date here is a calendar date. Glue runs in UTC; a laptop may not, and a session
        # in another zone moves a midnight timestamp onto the previous day.
        .config("spark.sql.session.timeZone", "UTC")
    )
    if local:
        # The worker must be the driver's own interpreter: Spark otherwise starts whatever
        # `python3` is first on PATH and refuses the job when its minor version differs.
        os.environ.setdefault("PYSPARK_PYTHON", sys.executable)
        builder = (builder.master("local[*]")
                   .config("spark.sql.shuffle.partitions", str(shuffle_partitions))
                   .config("spark.driver.memory", driver_memory)
                   .config("spark.ui.enabled", "false")
                   .config("spark.ui.showConsoleProgress", "false")
                   # Delta reads its log into 50 partitions by default, sized for a cluster.
                   .config("spark.databricks.delta.snapshotPartitions", "4"))
        if _delta_from_pip():
            from delta import configure_spark_with_delta_pip

            builder = configure_spark_with_delta_pip(builder)
    spark = builder.getOrCreate()
    spark.sparkContext.setLogLevel("ERROR")
    return spark


def _delta_from_pip():
    """True on a laptop with delta-spark from pip; False on Glue and in the Glue image, where
    Delta Lake is already on the classpath and the pip helper would fetch a second copy."""
    try:
        metadata.version("delta-spark")
        return True
    except metadata.PackageNotFoundError:
        return False


# --- the bucket ------------------------------------------------------------------------------------

def key_of(path, root):
    """The bucket key of a file under ``root``: forward slashes on every OS, as an S3 key has.

    ``str()`` of a relative path is ``raw_zone\\register\\...`` on Windows, and
    ``pending_batches`` takes the file name after the last ``/``, so every batch would be refused.
    """
    return path.relative_to(root).as_posix()


class LocalBucket:
    """A folder standing in for S3. A rename on one filesystem is atomic; an S3 move is not."""

    def __init__(self, root):
        self.root = Path(root).resolve()

    def uri(self, key):
        return str(self.root / key)

    def list(self, prefix):
        base = self.root / prefix
        if not base.exists():
            return []
        return sorted(key_of(p, self.root) for p in base.rglob("*") if p.is_file())

    def move(self, source, target):
        destination = self.root / target
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(self.root / source), str(destination))

    def put_text(self, key, text):
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


class S3Bucket:
    """S3 has no rename: a move is a copy and then a delete, and a crash between them leaves the
    object in both places. The next run then finds the batch in raw_zone again, MERGEs it as a
    no-op (nothing in it is newer than what is stored) and moves it onto the same archive key."""

    def __init__(self, bucket):
        import boto3

        self.client = boto3.client("s3")
        self.bucket = bucket

    def uri(self, key):
        return f"s3://{self.bucket}/{key}"

    def list(self, prefix):
        keys = []
        for page in self.client.get_paginator("list_objects_v2").paginate(
                Bucket=self.bucket, Prefix=prefix.rstrip("/") + "/"):
            keys += [item["Key"] for item in page.get("Contents", [])]
        return sorted(keys)

    def move(self, source, target):
        self.client.copy_object(Bucket=self.bucket, Key=target,
                                CopySource={"Bucket": self.bucket, "Key": source})
        self.client.delete_object(Bucket=self.bucket, Key=source)

    def put_text(self, key, text):
        self.client.put_object(Bucket=self.bucket, Key=key, Body=text.encode("utf-8"))


def open_bucket(args):
    return LocalBucket(args.root) if args.local else S3Bucket(args.bucket)


def pending_batches(bucket, source):
    """{batch date: key} for every file waiting in raw_zone/<source>/.

    A file that does not carry a batch name -- a stray upload, a half-written ``.part`` -- stops
    the job rather than being skipped, because skipping it would merge a batch with a hole in it.
    """
    found = {}
    for key in bucket.list(f"{RAW}/{source}"):
        name = key.rsplit("/", 1)[-1]
        match = BATCH_FILE.match(name)
        if not match or match["source"] != source:
            raise ValueError(f"{key}: not a {source}_<YYYY-MM-DD>.csv batch file")
        found[date.fromisoformat(match["day"])] = key
    return found


# --- schemas: one per source, in the source's own column order and names -----------------------------

def _schema(fields):
    return T.StructType([T.StructField(name, kind, True) for name, kind in fields])


_LONG = T.LongType()
SCHEMAS = {
    "register": _schema([
        ("regcode", T.StringType()), ("type", T.StringType()), ("registered", T.DateType()),
        ("terminated", T.DateType()), ("closed", T.StringType()),
    ]),
    "insolvency_legal_person_proceeding": _schema([
        ("proceeding_id", T.StringType()), ("debtor_registration_number", T.StringType()),
        ("proceeding_started_on", T.DateType()), ("proceeding_ended_on", T.DateType()),
        ("proceeding_form", T.StringType()), ("proceeding_type", T.StringType()),
        ("proceeding_resolution_type", T.StringType()),
        ("entry_created_on", T.TimestampType()),
    ]),
    "financial_statements": _schema([
        ("id", _LONG), ("file_id", _LONG), ("legal_entity_registration_number", T.StringType()),
        ("source_schema", T.StringType()), ("source_type", T.StringType()),
        ("year", T.IntegerType()), ("year_started_on", T.DateType()),
        ("year_ended_on", T.DateType()), ("employees", T.IntegerType()),
        ("rounded_to_nearest", T.StringType()), ("currency", T.StringType()),
        # The stamp has from zero to three fractional digits, which no single pattern accepts.
        # It is read as text and cast, and the cast takes all four forms.
        ("created_at", T.StringType()),
    ]),
    "balance_sheets": _schema([(c, _LONG) for c in config.BALANCE_COLUMNS]),
    "income_statements": _schema([(c, _LONG) for c in config.INCOME_COLUMNS]),
}


def read_batch(spark, bucket, source, key):
    return spark.read.csv(
        bucket.uri(key), schema=SCHEMAS[source], sep=";", header=True, enforceSchema=False,
        mode="FAILFAST", dateFormat="yyyy-MM-dd", timestampFormat="yyyy-MM-dd HH:mm:ss",
        encoding="UTF-8",
    )


# --- Delta -------------------------------------------------------------------------------------------

def table_path(bucket, table):
    return bucket.uri(f"{LAKE}/{table}")


def is_delta(spark, path):
    from delta.tables import DeltaTable

    return DeltaTable.isDeltaTable(spark, path)


def commit_note(batch_date, **counts):
    return json.dumps({"batch_date": batch_date.isoformat(), **counts}, sort_keys=True)


def merge_batch(spark, path, frame, key, order, values, note, partition_by=None):
    """Write the first batch, MERGE every later one; return the commit's operation metrics.

    ``order`` ranks two versions of one key, newest last: column names, or SQL with ``{}.`` where
    the table alias goes. ``values`` are the columns whose change makes a restatement. A matched row is updated only when it ranks after the stored row and
    differs from it in at least one of ``values`` (null-safe), so a replayed or unchanged row is
    counted, but not written.
    """
    from delta.tables import DeltaTable

    if not is_delta(spark, path):
        writer = frame.write.format("delta").option("userMetadata", note)
        if partition_by:
            writer = writer.partitionBy(partition_by)
        writer.save(path)
    else:
        condition = " AND ".join(f"t.{c} = s.{c}" for c in key)
        if partition_by:
            # Naming the batch's partitions lets the MERGE read only those. The join condition
            # alone gives Delta nothing to prune on, so it scans every fiscal year in the table
            # looking for matches; either way it rewrites only the files where it finds one.
            years = sorted({row[0] for row in frame.select(partition_by).distinct().collect()})
            condition += f" AND t.{partition_by} IN ({', '.join(str(y) for y in years)})"
        newer = _ranks_after(order)
        changed = " OR ".join(f"NOT (t.{c} <=> s.{c})" for c in values)
        spark.conf.set("spark.databricks.delta.commitInfo.userMetadata", note)
        try:
            (DeltaTable.forPath(spark, path).alias("t")
             .merge(frame.alias("s"), condition)
             .whenMatchedUpdateAll(condition=f"({newer}) AND ({changed})")
             .whenNotMatchedInsertAll()
             .execute())
        finally:
            spark.conf.unset("spark.databricks.delta.commitInfo.userMetadata")
    last = DeltaTable.forPath(spark, path).history(1).collect()[0]
    return last.version, dict(last.operationMetrics or {})


def _term(item, alias):
    """An ordering term for one side: a column name, or an expression with ``{}.`` for the alias."""
    return item.format(alias) if "{}" in item else f"{alias}.{item}"


def _ranks_after(order):
    """SQL for: s ranks strictly after t on the terms in ``order``, compared left to right."""
    clauses = []
    for i, term in enumerate(order):
        ties = [f"{_term(c, 't')} <=> {_term(c, 's')}" for c in order[:i]]
        clauses.append(" AND ".join(ties + [f"{_term(term, 's')} > {_term(term, 't')}"]))
    return " OR ".join(f"({c})" for c in clauses)


def keep_winner(frame, key, order):
    """One row per key: the one ranking last on ``order``. Returns (frame, rows dropped)."""
    from pyspark.sql.window import Window

    terms = [F.expr(c.replace("{}.", "")) if "{}" in c else F.col(c) for c in order]
    window = Window.partitionBy(*key).orderBy(*[t.desc_nulls_last() for t in terms])
    ranked = frame.withColumn("_rank", F.row_number().over(window))
    kept = ranked.filter("_rank = 1").drop("_rank").cache()
    return kept, frame.count() - kept.count()


def batch_versions(spark, path):
    """[(batch date, version)] from the table's history, oldest first.

    Several versions may carry one batch date -- a re-run of a batch commits a no-op MERGE -- and
    the last of them is the one an as-of read wants.
    """
    from delta.tables import DeltaTable

    rows = DeltaTable.forPath(spark, path).history().select("version", "userMetadata").collect()
    pairs = []
    for row in rows:
        if row.userMetadata:
            pairs.append((date.fromisoformat(json.loads(row.userMetadata)["batch_date"]),
                          row.version))
    return sorted(pairs, key=lambda pair: (pair[0], pair[1]))


def version_as_of(versions, day):
    """The newest version whose batch date is on or before ``day``; None before the first."""
    eligible = [version for batch, version in versions if batch <= day]
    return max(eligible) if eligible else None


def read_as_of(spark, path, versions, day):
    version = version_as_of(versions, day)
    if version is None:
        return None
    return spark.read.format("delta").option("versionAsOf", version).load(path)
