"""Create the database in the local SQL Server container and apply the T-SQL DDL to it.

The DDL is ``azure-sql/azure-sql-create-tables.sql``.

On Azure this script has no counterpart that runs as a job. The database is provisioned outside
T-SQL, with a service tier, and the DDL is applied once by someone holding CREATE rights; none of
the four jobs holds them. Locally both halves have to happen somewhere, and this is where.

WHAT IT DOES TO THE FILE, WHICH IS AS LITTLE AS POSSIBLE
--------------------------------------------------------
It splits the file on lines that hold only ``GO`` and sends each batch as it stands. Nothing is
rewritten, skipped or translated: there is one schema file, it is the one written for Azure SQL,
and the local run executes it. The AWS sibling's ``apply_ddl.py`` had to skip one line
(``create database``) because DuckDB has no equivalent; the T-SQL file does not contain that line
at all, so nothing here has to know about it.

``GO`` is split here because it is not T-SQL. It is the batch separator of sqlcmd and SSMS, the
server never sees it, and pyodbc would pass it through as a syntax error. The split follows
sqlcmd's rule -- ``GO`` alone on its line, any case -- so this file and ``sqlcmd -i`` read the
DDL into the same batches.

WHY THE DATABASE IS CREATED IN AUTOCOMMIT AND THE SCHEMA IS NOT
---------------------------------------------------------------
``CREATE DATABASE`` cannot run inside a user transaction, so it is sent on a separate connection
to ``master`` with autocommit on. Everything after it runs on a connection to the new database
with autocommit OFF and one commit at the end, because T-SQL DDL is transactional: if the fifth
batch fails, the rollback takes the first four with it and the database is left empty rather than
half-built. Redshift could not have given this file that property -- its DDL commits as it goes,
and it cannot even create a database and the objects inside it from one session.

``--drop`` exists for the test harness and for a clean re-run: it forces other sessions off the
database (``SINGLE_USER WITH ROLLBACK IMMEDIATE``) and drops it before creating it again. That is
a local-only verb; it is never pointed at anything but the container.

    python local-development/apply_ddl.py                   # create db_credit_mailer and apply
    python local-development/apply_ddl.py --drop            # start from an empty database
"""

import argparse
import logging
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(PROJECT, "python-jobs"))

import azure_common as az  # noqa: E402  (the path above is what makes it importable)

LOG = logging.getLogger("apply_ddl")

DEFAULT_DDL = os.path.join(PROJECT, "azure-sql", "azure-sql-create-tables.sql")

# sqlcmd's separator: GO on a line of its own, optionally followed by whitespace, any case.
# sqlcmd also accepts a repeat count (`GO 5`); this DDL never uses one, and a batch run five times
# would be a CREATE TABLE failing four times, so a count is refused rather than honoured.
_GO = re.compile(r"^[ \t]*GO[ \t]*$", re.IGNORECASE | re.MULTILINE)
_GO_WITH_COUNT = re.compile(r"^[ \t]*GO[ \t]+\d+[ \t]*$", re.IGNORECASE | re.MULTILINE)

EXPECTED_TABLES = 9
EXPECTED_ARMS = 18


def batches(text):
    """The DDL as a list of batches, split on GO lines, with empty and comment-only ones dropped.

    A batch holding only comments is dropped because the driver has nothing to execute and some
    drivers report that as an error rather than a no-op.
    """
    if _GO_WITH_COUNT.search(text):
        raise ValueError("the DDL uses `GO <count>`, which would run a batch more than once")
    kept = []
    for batch in _GO.split(text):
        code = "\n".join(line for line in batch.splitlines()
                         if line.strip() and not line.strip().startswith("--"))
        if code.strip():
            kept.append(batch.strip("\n"))
    return kept


def create_database(database, drop):
    """Create `database` on the server, dropping it first when asked. Autocommit, on master."""
    master = az.connect_sql("master", autocommit=True)
    try:
        cursor = master.cursor()
        if drop:
            cursor.execute("IF DB_ID(?) IS NOT NULL "
                           "ALTER DATABASE [{0}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE"
                           .format(database), database)
            cursor.execute("DROP DATABASE IF EXISTS [{0}]".format(database))
            LOG.info("dropped %s", database)
        cursor.execute("IF DB_ID(?) IS NULL CREATE DATABASE [{0}]".format(database), database)
    finally:
        master.close()


def drop_database(database):
    """Drop `database`. Used by the test harness's teardown."""
    master = az.connect_sql("master", autocommit=True)
    try:
        cursor = master.cursor()
        cursor.execute("IF DB_ID(?) IS NOT NULL "
                       "ALTER DATABASE [{0}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE"
                       .format(database), database)
        cursor.execute("DROP DATABASE IF EXISTS [{0}]".format(database))
    finally:
        master.close()


def apply(database=az.DEFAULT_DATABASE, ddl_path=DEFAULT_DDL, drop=False):
    """Create the database, run every batch in one transaction, and return (tables, arms)."""
    with open(ddl_path, encoding="utf-8") as handle:
        text = handle.read()
    parts = batches(text)

    create_database(database, drop)
    conn = az.connect_sql(database)
    try:
        cursor = conn.cursor()
        for number, batch in enumerate(parts, start=1):
            az.run(cursor, batch, log=LOG)
            # The last batch is the seed check, a SELECT; its rows are logged rather than
            # discarded, since they are the evidence the seed landed.
            while True:
                if cursor.description:
                    for row in cursor.fetchall():
                        LOG.info("   batch %s: %s", number, tuple(row))
                if not cursor.nextset():
                    break
        conn.commit()
        tables = cursor.execute(
            "SELECT table_schema, table_name FROM information_schema.tables "
            "WHERE table_type = 'BASE TABLE' ORDER BY table_schema, table_name").fetchall()
        arms = cursor.execute("SELECT COUNT(*) FROM processed_zone.dim_offer_arm").fetchone()[0]
    except Exception:
        # Guarded: a failed rollback would otherwise replace the batch error it is cleaning up.
        az.rollback(conn, log=LOG)
        raise
    finally:
        conn.close()

    for schema, table in tables:
        LOG.info("   %s.%s", schema, table)
    LOG.info("applied %s to %s in %s batches: %s tables, dim_offer_arm seeded with %s arms",
             os.path.relpath(ddl_path, PROJECT), database, len(parts), len(tables), arms)
    if len(tables) != EXPECTED_TABLES or arms != EXPECTED_ARMS:
        raise ValueError("expected {0} tables and {1} arms, found {2} and {3}"
                         .format(EXPECTED_TABLES, EXPECTED_ARMS, len(tables), arms))
    return tables, arms


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ddl", default=DEFAULT_DDL, help="the schema file to apply")
    parser.add_argument("--database", default=az.DEFAULT_DATABASE,
                        help="database to create and fill (default: %(default)s)")
    parser.add_argument("--drop", action="store_true",
                        help="drop the database first, so the run starts empty")
    args = parser.parse_args()
    apply(args.database, args.ddl, args.drop)


if __name__ == "__main__":
    main()
