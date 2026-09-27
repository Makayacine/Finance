"""The two emulators, waited for rather than slept for, and the scratch resources tests own.

Every test in this folder that touches storage or the warehouse talks to the real Azurite and the
real SQL Server started by ``docker-compose.yml``, through the same clients the jobs use. Nothing is
mocked, patched or substituted: no SQLite and no DuckDB stands in for T-SQL. DuckDB appears in two
places only, and in neither is it the warehouse under test -- it is the MySQL stand-in the AWS
sibling built, reused unchanged, and in one parity test it is the AWS pipeline's own warehouse,
built by the AWS jobs, which the SQL Server star is compared against.

WAITING, NOT SLEEPING
---------------------
SQL Server opens its port several seconds before it will accept a login, and a fixed sleep is
either too short on a cold start or wasted on a warm one. :func:`services` polls both emulators
with a real request -- a login and ``SELECT 1``; a table listing and a container listing -- until
each answers or a deadline passes. ``docker compose up -d --wait`` already blocks on the same
healthchecks, so on the documented path the first poll succeeds; the poll is for the other paths.

If a service never answers, the tests ERROR with a message saying how to start it. They do not
skip: a suite that reports green because the warehouse was not there has tested nothing, and the
AWS sibling's suite -- which needs neither container -- is the one to run for that.

ISOLATION
---------
Each module gets its own database, watermark table and landing container, named with a random
suffix and removed at the end of the session. So the suite never touches ``db_credit_mailer``,
``IncrementalLoadConfigurations`` or ``credit-mailer-lab`` -- the names the README's manual run
uses -- and a developer's own four-run demo survives a test run.
"""

import importlib.util
import os
import sys
import time
import uuid

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JOBS = os.path.join(PROJECT_ROOT, "python-jobs")
AWS_PROJECT = os.path.join(os.path.dirname(PROJECT_ROOT), "credit-mailer-watermark-glue-redshift")
SOURCE_GZIP = os.path.join(AWS_PROJECT, "data", "adcontentworth_qje.tab.gz")

# The compose file's default SA password, which is a local-only test value; MSSQL_SA_PASSWORD
# overrides both. The two connection strings follow .env.example and are only defaults: a shell
# that already exports them keeps its own.
SA_PASSWORD = os.environ.get("MSSQL_SA_PASSWORD", "CreditMailer-Local-2003")
os.environ.setdefault(
    "AZURE_SQL_CONNECTION_STRING",
    "DRIVER={ODBC Driver 18 for SQL Server};SERVER=127.0.0.1,1433;UID=sa;PWD={%s};"
    "TrustServerCertificate=yes" % SA_PASSWORD)
os.environ.setdefault("AZURE_STORAGE_CONNECTION_STRING", "UseDevelopmentStorage=true")

if JOBS not in sys.path:
    sys.path.insert(0, JOBS)

import azure_common as az  # noqa: E402  (JOBS on sys.path is what makes it importable)

START_HINT = ("start the emulators first: `docker compose up -d --wait` from "
              "credit-mailer-watermark-azure-sql/")


def load(relative_path, module_name):
    """Import a file of this project by path. Hyphenated names cannot be imported any other way."""
    if module_name in sys.modules:
        return sys.modules[module_name]
    path = os.path.join(PROJECT_ROOT, *relative_path.split("/"))
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _wait(label, probe, deadline_seconds):
    """Call `probe` until it returns without raising, or fail the session with the last error."""
    deadline = time.time() + deadline_seconds
    last = None
    while time.time() < deadline:
        try:
            probe()
            return
        except Exception as exc:                                    # any refusal means "not yet"
            last = exc
            time.sleep(1.0)
    pytest.fail("{0} did not answer within {1}s ({2}); {3}".format(
        label, deadline_seconds, str(last).splitlines()[0][:200] if last else "no attempt",
        START_HINT), pytrace=False)


def _sql_ready():
    conn = az.connect_sql("master", autocommit=True)
    try:
        assert conn.cursor().execute("SELECT 1").fetchone()[0] == 1
    finally:
        conn.close()


def _storage_ready():
    from azure.data.tables import TableServiceClient
    from azure.storage.blob import BlobServiceClient

    text = az.connection_string(az.STORAGE_CONNECTION_ENV)
    # retry_total=0: a refused connection should come back to this loop at once rather than sit
    # in the SDK's own exponential backoff.
    list(BlobServiceClient.from_connection_string(text, retry_total=0).list_containers())
    list(TableServiceClient.from_connection_string(text, retry_total=0).list_tables())


@pytest.fixture(scope="session")
def services():
    """Both emulators, answering. SQL Server gets longer: its first start recovers system dbs."""
    _wait("SQL Server on 127.0.0.1:1433", _sql_ready, 180)
    _wait("Azurite on 127.0.0.1:10000/10002", _storage_ready, 60)
    return True


@pytest.fixture(scope="session")
def builder():
    """The AWS sibling's source builder -- the MySQL stand-in, reused unchanged."""
    return az.load_aws_file("local-development/build_source_db.py", "aws_build_source_db")


@pytest.fixture(scope="session")
def source_tables(builder):
    """The committed deposit, hash-verified, minted and split once for the whole session."""
    return builder.split(builder.mint_and_cast(builder.read_source(SOURCE_GZIP)))


class Scratch(object):
    """Uniquely named resources, created on request and removed at session end."""

    def __init__(self):
        self.apply_ddl = load("local-development/apply_ddl.py", "azure_apply_ddl")
        self.seeder = load("table-storage/write-to-table-storage.py",
                           "azure_write_to_table_storage")
        self.databases, self.tables, self.containers = [], [], []

    @staticmethod
    def _suffix():
        return uuid.uuid4().hex[:10]

    def database(self):
        """A fresh database with the T-SQL DDL applied, exactly as apply_ddl.py applies it."""
        name = "cm_test_" + self._suffix()
        self.databases.append(name)
        self.apply_ddl.apply(name, drop=True)
        return name

    def config_table(self):
        """A fresh watermark table holding the two seeded entities."""
        name = "Watermark" + self._suffix()
        self.tables.append(name)
        self.seeder.write(name, create_table=True)
        return name

    def container(self):
        """A fresh, empty landing container."""
        name = "cm-test-" + self._suffix()
        self.containers.append(name)
        az.blob_container(name).create_container()
        return name

    def close(self):
        from azure.data.tables import TableServiceClient

        tables = TableServiceClient.from_connection_string(
            az.connection_string(az.STORAGE_CONNECTION_ENV))
        for name in self.tables:
            tables.delete_table(name)
        for name in self.containers:
            az.blob_container(name).delete_container()
        for name in self.databases:
            self.apply_ddl.drop_database(name)


@pytest.fixture(scope="session")
def scratch(services):
    resources = Scratch()
    yield resources
    resources.close()
