"""Extract one source table to the Blob landing zone, driven by a watermark in Azure Table Storage.

A plain Python job, the Azure counterpart of ``glue-jobs/mysql-extraction.py`` in the AWS sibling.
The source is the same MySQL database, the SQL is the same text, the CSV is the same bytes and the
watermark arithmetic is the same function -- imported from the AWS job, not copied from it::

      MySQL  credit_mailer.<table_name>
        ->   SELECT, with a watermark predicate when the config entity says so    build_query()
        ->   csv.DictWriter into io.StringIO                                      to_csv()
        ->   one blob at credit-mailer-lab/raw_landing_zone/credit_mailer_db/<table>/data.csv
        ->   Table Storage IncrementalLoadConfigurations.last_extracted_value     next_watermark()

Three edges are new and nothing else is: the config read, the landing write and the watermark
write. Each is a function below, and each is where the Azure service differs from the AWS one in a
way that can change which rows move.

THE LESSON IS THE AWS JOB'S, UNCHANGED
--------------------------------------
Nothing in this file knows how far the last run got. It reads one entity per source table, and it
writes the answer back when it finishes. ``client_attributes`` is the control: a CRM snapshot with
no event time, so no ``load_column``, so a full load of all 58,168 rows on every run. The staging
comes from the SOURCE growing, a wave at a time, and four runs produce 4,974 -> 20,996 -> 32,198 ->
0 rows with the watermark going '1' -> '2' -> '3' -> '3'. Run 4 is the proof that the stored value
is READ and not merely written. The AWS module docstring argues all of this at length, including
the string-compared watermark that is safe for waves 1-3 and a trap at wave 10; it is not repeated
here because the code that makes those decisions is that module's code.

WHAT TABLE STORAGE CHANGES
--------------------------
*   **An absent property, not a null.** Table Storage drops a property whose value is None, so
    ``client_attributes`` comes back with no ``load_column`` key and a fresh ``mail_offers`` with no
    ``last_extracted_value``. Both are read with ``.get()``, which restores the None the AWS logic
    branches on. Indexing with ``[]`` would raise KeyError on exactly the two entities whose
    absence is the lesson.
*   **The key is two-part.** PartitionKey is the source database, RowKey the table. A missing
    entity raises -- the AWS job's fix (4), kept: "no row" and "the read failed" are different
    failures and must not both become ``(None, None)``.
*   **The watermark write is conditional on the read.** The entity's ETag is captured when the
    config is read and the write is made ``IfNotModified`` against it. DynamoDB's ``update_item``
    in the AWS job is unconditional, so a reset or a second run that touched the row while this
    one was extracting would be overwritten with a value computed from a state that no longer
    exists. Here the write fails instead, after the landing object is already in place -- which
    is the safe side of the landing-then-watermark ordering: the next run repeats this extract,
    and the keyed MERGE downstream makes the repeat harmless. It costs one keyword argument.
*   **MERGE, not REPLACE, for the write.** ``last_extracted_value`` is the only property this job
    owns. A REPLACE would rewrite ``load_column`` too, from whatever this job happened to read,
    and turn a watermark update into a configuration write. MERGE is the SET of the AWS job's
    ``UpdateExpression``.

WHAT BLOB STORAGE CHANGES
-------------------------
Almost nothing, which is the point of keeping the S3 key: the container is ``credit-mailer-lab``
and the blob name is the S3 key byte for byte. ``overwrite=True`` is the fixed-key design -- the
landing zone holds only the latest extract of a table, so the chain must stay sequential -- and
the upload is ``text/csv`` in UTF-8, encoded here because the bytes are what the loader reads.
Run 4's header-only CSV is a blob of 379 bytes, not an empty one; see the AWS ``to_csv()``.

The container is not created here. The AWS job did not create its bucket either: a job that
creates the thing it writes into holds a right it uses on no normal day.

THE SOURCE
----------
``--source-db PATH`` opens the AWS sibling's DuckDB stand-in, read-only, exactly as that job's
``--local`` does. Without it the job connects to MySQL with pymysql, reading ``MYSQL_HOST``,
``MYSQL_USER`` and ``MYSQL_PASSWORD`` from the environment -- the Azure equivalent of the Secrets
Manager read, for the reason ``azure_common`` gives: a Key Vault reference arrives as an
environment variable, and a password on the command line is readable by every user on the host.

Local acceptance run, with the containers up and the variables in .env.example exported::

    python python-jobs/mysql-extraction.py --self-check
    python python-jobs/mysql-extraction.py --table_name mail_offers --load_type incremental \\
        --source-db _localrun/source.duckdb
"""

import argparse
import logging
import sys

import azure_common as az

LOG = logging.getLogger("mysql_extraction_azure")

# The AWS job, loaded once. Everything this file does not have to change is taken from it.
AWS = az.load_aws_job("mysql-extraction.py", "aws_mysql_extraction")
build_query = AWS.build_query
next_watermark = AWS.next_watermark
to_csv = AWS.to_csv
extract = AWS.extract

DEFAULT_DATABASE = AWS.DEFAULT_DATABASE    # the MySQL database: credit_mailer

# The column lists live in the raw-ingestion job on the AWS side; the self-check borrows them.
AWS_TABLES = az.load_aws_job("redshift-raw-ingestion.py", "aws_redshift_raw_ingestion").TABLES


def fetch_configuration(table_client, table_name):
    """``(load_column, last_extracted_value, etag)`` for this run's table.

    ``.get()`` on both properties, because Table Storage stores a None by not storing it. See the
    module docstring: the absence IS the configuration for ``client_attributes``.
    """
    from azure.core.exceptions import ResourceNotFoundError

    try:
        item = table_client.get_entity(partition_key=az.CONFIG_PARTITION,
                                       row_key=az.check_row_key(table_name))
    except ResourceNotFoundError:
        raise KeyError("{0} has no entity in {1} (PartitionKey {2!r}); seed it with "
                       "table-storage/write-to-table-storage.py"
                       .format(table_name, table_client.table_name, az.CONFIG_PARTITION))
    return item.get("load_column"), item.get("last_extracted_value"), item.metadata["etag"]


def update_last_extracted_value(table_client, table_name, value, etag):
    """Write the watermark back, MERGE mode, conditional on the ETag read before the extract.

    Called only after the landing blob's upload has returned. The ordering is the AWS job's and
    so is the argument for it: a crash between the two repeats the extract, which is harmless
    because the key is fixed and the downstream MERGE is keyed; the other order moves the
    watermark past rows that never landed.
    """
    from azure.core import MatchConditions
    from azure.data.tables import UpdateMode

    table_client.update_entity(
        {"PartitionKey": az.CONFIG_PARTITION, "RowKey": table_name,
         "last_extracted_value": value},
        mode=UpdateMode.MERGE, etag=etag, match_condition=MatchConditions.IfNotModified)
    LOG.info("watermark for %s is now %r", table_name, value)


def connect_source(args):
    """The source: the DuckDB stand-in when --source-db is given, MySQL otherwise.

    DuckDB read-only, as in the AWS job: an extractor has no business writing to its source, and
    read-only lets two extracts share the file.
    """
    if args.source_db:
        import duckdb

        return duckdb.connect(args.source_db, read_only=True)

    import pymysql

    return pymysql.connect(host=az.connection_string("MYSQL_HOST"),
                           user=az.connection_string("MYSQL_USER"),
                           password=az.connection_string("MYSQL_PASSWORD"),
                           database=args.database)


def write_landing_object(container_client, table_name, csv_data):
    """Overwrite the table's single blob in the landing zone. Returns its URL."""
    from azure.storage.blob import ContentSettings

    blob = container_client.get_blob_client(az.landing_blob_name(table_name))
    blob.upload_blob(csv_data.encode("utf-8"), overwrite=True,
                     content_settings=ContentSettings(content_type="text/csv",
                                                      content_encoding="utf-8"))
    return blob.url


def self_check():
    """The AWS job's self-check, then the three things this file adds. No network, no service."""
    # 1. The AWS decisions -- the predicate in four states, the watermark advance and its guard,
    #    the header-only CSV, NULL as an empty field -- are this job's decisions too, because they
    #    are the same functions. Their own assertions are run rather than restated.
    AWS.self_check()
    assert build_query is AWS.build_query and next_watermark is AWS.next_watermark
    assert to_csv is AWS.to_csv and extract is AWS.extract

    # 2. The shared surface: database replacement, table-name legality, landing key == S3 key.
    az.self_check()

    # 3. The absent-property reading. A Table Storage entity with no load_column must read as
    #    None, which is what sends client_attributes down the full-load path; a dict stands in for
    #    the entity because TableEntity is a dict subclass and .get() is the whole of the contract.
    seeded = {"PartitionKey": az.CONFIG_PARTITION, "RowKey": "client_attributes"}
    assert seeded.get("load_column") is None and seeded.get("last_extracted_value") is None

    # 4. Run 4's blob is a header and not an empty object, measured in bytes: the 32 column
    #    names, 31 commas and a newline.
    header_only = to_csv(list(AWS_TABLES["mail_offers"]["columns"]), [])
    assert len(header_only.encode("utf-8")) == 379, len(header_only.encode("utf-8"))

    LOG.info("self-check passed: the AWS job's predicate, watermark and CSV decisions, the "
             "absent-property reading of a None, and a 379-byte header-only blob for run 4")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    # Underscored, as the AWS job's are, because the chain runner passes the Step Functions
    # definition's argument names unchanged.
    parser.add_argument("--table_name",
                        help="the source table to extract: mail_offers or client_attributes")
    parser.add_argument("--load_type", choices=("full_load", "incremental"),
                        help="full_load ignores the watermark entirely; incremental reads it and "
                             "writes it back")
    parser.add_argument("--source-db", default=None,
                        help="the DuckDB stand-in for MySQL, as built by the AWS sibling's "
                             "local-development/build_source_db.py. Without it the job reads "
                             "MySQL, with MYSQL_HOST/MYSQL_USER/MYSQL_PASSWORD from the "
                             "environment")
    parser.add_argument("--database", default=DEFAULT_DATABASE,
                        help="MySQL database name (default: %(default)s)")
    parser.add_argument("--container", default=az.DEFAULT_CONTAINER,
                        help="landing-zone container (default: %(default)s). The blob name "
                             "under it is fixed at {0}/<table>/data.csv".format(az.LANDING_PREFIX))
    parser.add_argument("--config-table", default=az.CONFIG_TABLE,
                        help="Table Storage table holding the watermark (default: %(default)s)")
    parser.add_argument("--dry-run", action="store_true",
                        help="run the SELECT and build the CSV, then write neither the blob nor "
                             "the watermark")
    parser.add_argument("--self-check", action="store_true",
                        help="assert the predicate builder, the watermark advance and the "
                             "Table Storage reading, then exit; no network, no input")
    args, _ = parser.parse_known_args()

    if args.self_check:
        self_check()
        return
    if not args.table_name:
        parser.error("--table_name is required unless --self-check")
    if not args.load_type:
        parser.error("--load_type is required unless --self-check")

    # Read BEFORE the source is opened, as in the AWS job, so the disagreement below costs one
    # entity read and never touches the source or the landing zone.
    table_client = az.config_table(args.config_table)
    load_column, last_extracted_value, etag = fetch_configuration(table_client, args.table_name)

    if args.load_type == "incremental" and not load_column:
        # The AWS job's exit(1), kept for the AWS job's reason: the chain and the config entity
        # disagree about what kind of table this is, and neither a full extract nor an empty one
        # is a safe reading of that.
        LOG.error("%s is configured with no load_column but was asked for an incremental load. "
                  "The chain and %s disagree; neither a full extract nor an empty one is a safe "
                  "reading of that. Exiting.", args.table_name, args.config_table)
        sys.exit(1)

    sql = build_query(args.table_name, args.load_type, load_column, last_extracted_value)
    LOG.info("%s | %s | load_column=%r last_extracted_value=%r%s", args.table_name,
             args.load_type, load_column, last_extracted_value,
             " | DRY RUN, nothing will be written" if args.dry_run else "")
    LOG.info("sql: %s", sql)

    connection = None
    try:
        connection = connect_source(args)
        fieldnames, rows = extract(connection, sql)
    finally:
        if connection is not None:
            connection.close()

    csv_data = to_csv(fieldnames, rows)
    if args.dry_run:
        LOG.info("dry run: %s rows, %s columns, %s bytes of CSV, discarded. The watermark stays "
                 "at %r", len(rows), len(fieldnames), len(csv_data.encode("utf-8")),
                 last_extracted_value)
        return

    destination = write_landing_object(az.blob_container(args.container), args.table_name,
                                       csv_data)
    LOG.info("%s rows, %s columns -> %s", len(rows), len(fieldnames), destination)
    if not rows:
        LOG.info("the extract was empty, so the blob holds a header and no rows. On an "
                 "incremental table this is the source not having grown since the last run, "
                 "which is the one observation that proves the stored watermark was read")

    # Landing blob first, watermark second.
    if args.load_type == "incremental":
        new_value = next_watermark(rows, load_column, last_extracted_value)
        if new_value == last_extracted_value:
            LOG.info("watermark for %s stays at %r: nothing new was extracted",
                     args.table_name, last_extracted_value)
        else:
            update_last_extracted_value(table_client, args.table_name, new_value, etag)


if __name__ == "__main__":
    main()
