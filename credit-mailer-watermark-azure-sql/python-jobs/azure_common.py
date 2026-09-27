"""The Azure connection surface shared by the four jobs, and the one door into the AWS sibling.

This is the Azure counterpart of ``warehouse_common.py`` in
``credit-mailer-watermark-glue-redshift/glue-jobs/``, with one extra responsibility that module
never had: loading the AWS jobs themselves. The port reuses their engine-neutral logic rather than
copying it -- the predicate builder, the watermark advance, the CSV writer, the column lists, the
MERGE generators, the bandit arithmetic -- so the only code in ``python-jobs/`` that is new is the
code that talks to a different service. Where the two pipelines agree, they agree because they are
running the same function, not because two copies were kept in step.

WHAT THAT COSTS, STATED UP FRONT
--------------------------------
This folder depends on its AWS sibling being next to it, at
``../credit-mailer-watermark-glue-redshift``.
Moving one without the other breaks every job here at import time, loudly, with the path it looked
for. That is the trade taken: a fork of ~2,000 lines of reviewed logic would drift silently, and a
missing sibling does not. A deployment of the jobs needs the four AWS job files and
``warehouse_common.py`` beside them, and the seeder needs ``dynamodb/write-to-dynamo.py`` for the
declared configuration; nothing else from that tree is imported outside the tests.

Two smaller consequences, both deliberate. The reused functions log under the logger name of the
module they live in -- a verdict line from the refinery says ``glue_refinery_path3`` -- which is
the truthful answer to "which code printed this". And the AWS modules are loaded under names
prefixed ``aws_``, so the Azure job and the AWS job of the same file name can sit in one process,
which is what the test suite does.

WHAT IS DELIBERATELY NOT IN HERE
--------------------------------
The same rule as ``warehouse_common.py``: nothing that knows what a table is or what a step does.
No column lists, no MERGE text, no watermark arithmetic. The landing-zone layout and the
config-table address ARE here, because three files have to agree on them and a disagreement between
the writer and the reader of a blob name is a load of nothing that reports success.

CREDENTIALS COME FROM THE ENVIRONMENT, NEVER FROM ARGV
------------------------------------------------------
The AWS jobs read a Secrets Manager secret by name. The Azure equivalent is a Key Vault reference
resolved into an app setting by whatever hosts the job, which reaches the process as an environment
variable -- so that is where these are read from:

    AZURE_SQL_CONNECTION_STRING      ODBC string naming the server and how to authenticate
    AZURE_STORAGE_CONNECTION_STRING  the storage account holding the landing zone and the watermark

Not flags. A connection string on the command line is visible to every user on the host through the
process table, and the SQL one carries a password whenever it is not using a managed identity. The
database is NOT part of the SQL string's contract: jobs take ``--database`` (default
``db_credit_mailer``) and :func:`with_database` sets it, so one server string serves the harness,
which must first reach ``master`` to create the database, and every job, which must not.

Azurite is the local storage account and SQL Server 2022 in a container is the local server, and
both are reached through exactly these variables and exactly these clients. There is no ``--local``
flag on any Azure job: the emulators speak the same wire protocol as the services, so the only
thing that changes between the two is the value of two strings.

WHY THE CLIENT IMPORTS ARE INSIDE THE FUNCTIONS
-----------------------------------------------
``pyodbc`` needs a native ODBC driver manager to import at all, and the extraction job never opens
the warehouse. Imported at module scope, it would make the extractor need ODBC installed to run;
imported inside :func:`connect_sql`, each job pays for the client it actually opens. Same argument
``warehouse_common.py`` makes about ``redshift_connector`` and ``duckdb``.
"""

import importlib.util
import logging
import os
import re
import sys

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s - %(message)s")
LOG = logging.getLogger("azure_common")

# The Azure SDK logs every request's URL and headers at INFO through azure.core's http logging
# policy. With the root logger at INFO -- which the AWS modules set, and which this file keeps so
# the reused functions' lines still appear -- that is some twenty lines per HTTP call, burying the
# row counts that are the point of the log. Warnings and errors from the SDK still come through.
logging.getLogger("azure").setLevel(logging.WARNING)

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)
AWS_PROJECT = os.path.join(os.path.dirname(PROJECT_ROOT), "credit-mailer-watermark-glue-redshift")
AWS_JOBS = os.path.join(AWS_PROJECT, "glue-jobs")

SQL_CONNECTION_ENV = "AZURE_SQL_CONNECTION_STRING"
STORAGE_CONNECTION_ENV = "AZURE_STORAGE_CONNECTION_STRING"

DEFAULT_DATABASE = "db_credit_mailer"

# The landing zone. One container, and under it the same key the AWS pipeline writes to S3:
#
#     credit-mailer-lab / raw_landing_zone/credit_mailer_db/<table>/data.csv
#
# which is an ADLS Gen2 layout -- filesystem, then a directory path -- written through the Blob
# API. A hierarchical-namespace account accepts Blob API writes on the same paths, so the jobs
# need nothing Gen2-specific; Azurite has no DFS endpoint at all, so the Blob API is also the only
# one the local run can exercise.
#
# The container has a default where the AWS raw-ingestion job refused one for its bucket. That job
# argued that a default bucket name eventually belongs to somebody else, and it is right about S3,
# whose names are global. A container name is scoped to the storage account the connection string
# already selected, so the only account it can ever resolve in is this pipeline's own.
DEFAULT_CONTAINER = "credit-mailer-lab"
LANDING_PREFIX = "raw_landing_zone/credit_mailer_db"

# The watermark table. `incremental_load_configurations` is the DynamoDB name and it is not a
# legal Table Storage name: table names are 3-63 alphanumeric characters starting with a letter,
# and Azurite refuses the underscore exactly as the service does ("The specified resource name
# contains invalid characters"). Same words, CamelCased.
CONFIG_TABLE = "IncrementalLoadConfigurations"
_TABLE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9]{2,62}$")

# Every entity needs a PartitionKey and a RowKey, where the DynamoDB item had one key. The source
# database is the partition and the source table is the row, so the two configurations share a
# partition -- and that is a property, not a coincidence of naming: an entity-group transaction
# is limited to one partition, and being in one is what lets the seeder write both rows
# atomically. DynamoDB's batch_writer, which the AWS seeder uses, is not atomic.
CONFIG_PARTITION = "credit_mailer"

# Characters Table Storage forbids in a key. A source table name that contained one would be
# refused by the service at write time; checking it here makes that a named error at read time.
_KEY_FORBIDDEN = re.compile(r"[/\\#?\x00-\x1f\x7f-\x9f]")


def load_aws_job(file_name, module_name=None):
    """Import one file from the AWS sibling's ``glue-jobs/`` by path, and return the module.

    By path, because the files carry hyphens -- ``mysql-extraction.py`` -- which no import
    statement can name. The AWS tests pay the same cost the same way.

    ``glue-jobs/`` goes on ``sys.path`` first, because three of the four AWS jobs do
    ``from warehouse_common import ...`` at module scope. That module imports only the standard
    library at module scope, so loading it here pulls in neither boto3 nor duckdb nor
    redshift_connector; each of those is imported inside the AWS branch that uses it, which is
    code this port never calls.

    Cached in ``sys.modules`` under ``module_name`` so a second job in the same process gets the
    same module object rather than a second copy with its own globals.
    """
    module_name = module_name or "aws_" + os.path.splitext(file_name)[0].replace("-", "_")
    if module_name in sys.modules:
        return sys.modules[module_name]
    path = os.path.join(AWS_JOBS, file_name)
    if not os.path.isfile(path):
        raise ImportError("{0} is not there. The Azure jobs reuse the AWS jobs' logic and expect "
                          "credit-mailer-watermark-glue-redshift/ beside this folder; see "
                          "python-jobs/azure_common.py".format(path))
    if AWS_JOBS not in sys.path:
        sys.path.insert(0, AWS_JOBS)
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def load_aws_file(relative_path, module_name):
    """Like :func:`load_aws_job`, for a file elsewhere in the AWS tree (the seeder, the builder)."""
    if module_name in sys.modules:
        return sys.modules[module_name]
    path = os.path.join(AWS_PROJECT, *relative_path.split("/"))
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


# The AWS statement runner, reused as is: it logs a condensed copy of the statement and executes
# the original, commits nothing and catches nothing. pyodbc's cursor has the execute() and
# rowcount it relies on, so it is engine-neutral already.
run = load_aws_job("warehouse_common.py", "aws_warehouse_common").run


def connection_string(variable):
    """The value of one connection-string variable, or a ValueError that names it."""
    value = os.environ.get(variable)
    if not value:
        raise ValueError("{0} is not set. It holds a connection string, which is read from the "
                         "environment rather than a flag so it never appears in the process "
                         "table; for the local containers see .env.example".format(variable))
    return value


def _split_odbc(text):
    """Split an ODBC connection string into (key, value) pairs, respecting ``{braced}`` values.

    A naive split on ``;`` breaks on a password containing one, which ODBC allows inside braces.
    """
    pairs, key, value, depth, in_value = [], "", "", 0, False
    for char in text:
        if not in_value:
            if char == "=":
                in_value = True
            elif char == ";":
                key = ""
            else:
                key += char
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        if char == ";" and depth == 0:
            pairs.append((key.strip(), value))
            key, value, in_value = "", "", False
            continue
        value += char
    if in_value:
        pairs.append((key.strip(), value))
    return pairs


def with_database(odbc, database):
    """Return the ODBC string with its database set to `database`, replacing any already named.

    Replacing rather than appending because ODBC keeps the FIRST occurrence of a repeated keyword,
    so ``...;Database=master;Database=db_credit_mailer`` connects to master -- a job that then
    finds none of its tables, in a database it has rights to write in.
    """
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", database):
        raise ValueError("database name {0!r} is not a bare identifier".format(database))
    kept = [(k, v) for k, v in _split_odbc(odbc)
            if k.lower() not in ("database", "initial catalog")]
    return ";".join("{0}={1}".format(k, v) for k, v in kept) + ";DATABASE={0}".format(database)


def connect_sql(database=DEFAULT_DATABASE, autocommit=False):
    """Open the warehouse with pyodbc. The caller owns the transaction.

    ``autocommit=False`` is pyodbc's default and is passed anyway, because the whole transaction
    story of these jobs rests on it: the driver opens a transaction implicitly on the first
    statement, and ``conn.commit()`` / ``conn.rollback()`` end it. That is the ONE mechanism.

    The AWS jobs also send ``BEGIN TRANSACTION`` and ``COMMIT`` as statement text, because DuckDB
    needs them. Sent through pyodbc they would not be a harmless no-op the way they are inside
    Redshift's implicit transaction: T-SQL nests them. Measured on SQL Server 2022: ``BEGIN
    TRANSACTION`` inside the driver's implicit transaction takes ``@@TRANCOUNT`` to 2,
    ``conn.commit()`` then takes it to 1 and makes nothing durable, and closing the connection
    rolls the insert back -- a job that logged a successful commit and kept nothing. None of the
    Azure jobs sends either as text.
    """
    import pyodbc

    odbc = with_database(connection_string(SQL_CONNECTION_ENV), database)
    LOG.info("connecting to SQL database %s", database)
    return pyodbc.connect(odbc, autocommit=autocommit)


def blob_container(container=DEFAULT_CONTAINER):
    """A ContainerClient on the landing-zone container. The container is not created here."""
    from azure.storage.blob import BlobServiceClient

    service = BlobServiceClient.from_connection_string(connection_string(STORAGE_CONNECTION_ENV))
    return service.get_container_client(container)


def config_table(table=CONFIG_TABLE):
    """A TableClient on the watermark table. The table is not created here."""
    from azure.data.tables import TableServiceClient

    if not _TABLE_NAME.match(table):
        raise ValueError("{0!r} is not a legal Table Storage name: 3-63 letters and digits, "
                         "starting with a letter, and no underscores".format(table))
    service = TableServiceClient.from_connection_string(connection_string(STORAGE_CONNECTION_ENV))
    return service.get_table_client(table)


def landing_blob_name(table):
    """The one blob a table's extract is written to, overwritten every run."""
    return "{0}/{1}/data.csv".format(LANDING_PREFIX, table)


def check_row_key(value):
    """Refuse a value Table Storage would refuse as a key, before it reaches the service."""
    if not value or _KEY_FORBIDDEN.search(value):
        raise ValueError("{0!r} cannot be a Table Storage key".format(value))
    return value


def self_check():
    """Assert the pieces here that fail quietly. No network, no files, no service."""
    # 1. with_database replaces, never appends, and survives a braced value holding a ';'.
    odbc = "DRIVER={ODBC Driver 18 for SQL Server};SERVER=x;PWD={a;b};Database=master"
    rewritten = with_database(odbc, "db_credit_mailer")
    assert rewritten.lower().count("database=") == 1, rewritten
    assert rewritten.endswith(";DATABASE=db_credit_mailer"), rewritten
    assert "PWD={a;b}" in rewritten, "a braced value lost its semicolon"

    # 2. The DynamoDB name is not a Table Storage name, and the port's name is.
    assert not _TABLE_NAME.match("incremental_load_configurations")
    assert _TABLE_NAME.match(CONFIG_TABLE)

    # 3. The landing key is the S3 key, byte for byte, so the two pipelines' landing zones can be
    #    compared by listing them.
    aws = load_aws_job("redshift-raw-ingestion.py", "aws_redshift_raw_ingestion")
    assert LANDING_PREFIX == aws.LANDING_PREFIX
    assert landing_blob_name("mail_offers") == \
        "raw_landing_zone/credit_mailer_db/mail_offers/data.csv"

    for bad in ["a/b", "a#b", "", "a?b"]:
        try:
            check_row_key(bad)
        except ValueError:
            pass
        else:                                                       # pragma: no cover
            raise AssertionError("{0!r} was accepted as a key".format(bad))
    LOG.info("self-check passed: the database is replaced rather than appended, the table name "
             "is legal where the DynamoDB one is not, and the landing key is the S3 key")


if __name__ == "__main__":
    self_check()
