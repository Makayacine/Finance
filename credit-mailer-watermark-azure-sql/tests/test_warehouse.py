"""The load half: the four runs through SQL Server, and what it enforces that Redshift did not.

The AWS suite stops at the landing zone. Its warehouse jobs are covered by ``--self-check``, which
asserts on SQL text, and by a manual ``--local`` run against DuckDB. So porting its eighteen tests
proves the extract half of the port and nothing about the load, and the claim that matters --
"run 4 loads zero rows" -- is a claim about the warehouse. This file is where it is tested.

Everything runs against the real SQL Server container through pyodbc and the real Azurite through
the Azure SDKs, via the jobs' own command lines and ``main()`` functions. Nothing is mocked.

WHAT IS HERE
------------
*   **The four-run chain**, through ``orchestration/run-chain.py`` -- six subprocesses per run, as a
    state machine would start them -- with the warehouse measured after every run: the landing
    counts, the watermark, both raw tables, the dimension and the fact. Run 4 must extract nothing,
    load nothing and change nothing.
*   **The data rules the port had to re-earn in T-SQL**: wave 1's fourteen NULL treatment flags
    reaching the fact as NULL and not as the zeros ``CAST('' AS SMALLINT)`` would make of them,
    and the two BIT flags on ``dim_client`` keeping NULL where the raw SMALLINT is NULL.
*   **Parity with the AWS pipeline.** The AWS jobs, unmodified, are run through their own
    ``--local`` four-run sequence on DuckDB, and every table of the resulting star -- both raw
    tables, the dimension, the fact, the arm grid and both bandit tables -- is compared with the
    SQL Server star row for row and digit for digit. Where the two engines agree on every value,
    the T-SQL port is doing what the Redshift DDL and jobs do.
*   **The three engine differences in the port's favour**, each asserted rather than described:
    VARCHAR(16) is enforced, PRIMARY KEY is enforced, and TRUNCATE rolls back with the load.
*   **The Table Storage traps**: a MERGE-mode reset leaves the watermark in place where REPLACE
    clears it, and the watermark write refuses a stale ETag.
*   **The chain**: its states are the Step Functions definition's, in order, and a failing state
    stops it.

Run from the project root, with the emulators up::

    python -m pytest tests -q
"""

import csv
import importlib.util
import io
import json
import os
import subprocess
import sys
from decimal import Decimal

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JOBS = os.path.join(PROJECT_ROOT, "python-jobs")
AWS_PROJECT = os.path.join(os.path.dirname(PROJECT_ROOT), "credit-mailer-watermark-glue-redshift")
if JOBS not in sys.path:
    sys.path.insert(0, JOBS)

import azure_common as az  # noqa: E402  (JOBS on sys.path is what makes it importable)


def load_module(relative_path, module_name):
    """Import a file of this project by path. Hyphenated names cannot be imported any other way."""
    if module_name in sys.modules:
        return sys.modules[module_name]
    path = os.path.join(PROJECT_ROOT, *relative_path.split("/"))
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


extraction = load_module("python-jobs/mysql-extraction.py", "azure_mysql_extraction")
ingestion = load_module("python-jobs/azure-sql-raw-ingestion.py", "azure_sql_raw_ingestion")
processed = load_module("python-jobs/azure-sql-processed-layer.py", "azure_sql_processed_layer")
refinery = load_module("python-jobs/refinery-path3.py", "azure_refinery_path3")
chain_runner = load_module("orchestration/run-chain.py", "azure_run_chain")
apply_ddl = load_module("local-development/apply_ddl.py", "azure_apply_ddl")
seeder = load_module("table-storage/write-to-table-storage.py", "azure_write_to_table_storage")

SOURCE_THROUGH_WAVE = (1, 2, 3, 3)

# The four runs, as the warehouse should see them.
EXPECTED_LANDED = [4974, 20996, 32198, 0]              # rows in the mail_offers landing blob
EXPECTED_RAW_MAIL_OFFERS = [4974, 25970, 58168, 58168]  # rows in raw_zone.mail_offers
EXPECTED_FACT = [4974, 25970, 58168, 58168]             # rows in processed_zone.fact_mailer
EXPECTED_WATERMARKS = ["1", "2", "3", "3"]
SOURCE_ROW_COUNT = 58168

TREATMENT_FLAGS = processed.TREATMENT_FLAGS             # the 19 flags, from the AWS column table
WAVE_ONE_ROWS = 4974
WAVE_ONE_WHOLLY_NULL_FLAGS = 14
WAVE_ONE_NULL_CELLS = 69636                             # 14 x 4,974

# The posterior mean per arm after all three waves, as the AWS README publishes it (five places).
# Deterministic -- a function of the counts only, with no seed involved -- so it is asserted
# exactly rather than within a tolerance.
PUBLISHED_POSTERIOR = {
    "HIGH": ("0.05762", "0.05080", "0.04998", "0.04471", "0.04580", "0.04075"),
    "MEDIUM": ("0.15573", "0.16546", "0.15835", "0.12863", "0.15179", "0.15299"),
    "LOW": ("0.17793", "0.16865", "0.16786", "0.14031", "0.15455", "0.15030"),
}


def _sql(database):
    return az.connect_sql(database)


def _scalar(database, sql):
    conn = _sql(database)
    try:
        return conn.cursor().execute(sql).fetchone()[0]
    finally:
        conn.close()


def _rows(database, sql):
    conn = _sql(database)
    try:
        return [tuple(row) for row in conn.cursor().execute(sql).fetchall()]
    finally:
        conn.close()


def _run_main(module, argv):
    """Call a job's ``main()`` with flags on ``sys.argv``, as the runner would."""
    saved = sys.argv
    sys.argv = ["job"] + argv
    try:
        module.main()
    finally:
        sys.argv = saved


# --------------------------------------------------------------------------------------------
# THE FOUR RUNS, THROUGH THE CHAIN
# --------------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def chain(scratch, builder, source_tables, tmp_path_factory):
    """Run the six-state chain four times against a growing source; measure after every run."""
    source = str(tmp_path_factory.mktemp("chain") / "source.duckdb")
    database = scratch.database()
    config_table = scratch.config_table()
    container = scratch.container()

    built = None
    runs = []
    for through_wave in SOURCE_THROUGH_WAVE:
        if through_wave != built:
            builder.write_duckdb(source, builder.stage(source_tables, through_wave))
            built = through_wave
        code = subprocess.call([sys.executable, os.path.join(PROJECT_ROOT, "orchestration",
                                                             "run-chain.py"),
                                "--source-db", source, "--database", database,
                                "--config-table", config_table, "--container", container])
        assert code == 0, "the chain failed on the run through wave %s" % through_wave

        landed = az.blob_container(container).get_blob_client(
            az.landing_blob_name("mail_offers")).download_blob().readall().decode("utf-8")
        watermarks = az.config_table(config_table)
        runs.append({
            "landed": len(landed.splitlines()) - 1,
            "watermark": watermarks.get_entity(az.CONFIG_PARTITION,
                                               "mail_offers").get("last_extracted_value"),
            "client_watermark": dict(watermarks.get_entity(az.CONFIG_PARTITION,
                                                           "client_attributes")),
            "raw_mail_offers": _scalar(database, "SELECT COUNT(*) FROM raw_zone.mail_offers"),
            "raw_client_attributes": _scalar(database,
                                             "SELECT COUNT(*) FROM raw_zone.client_attributes"),
            "dim_client": _scalar(database, "SELECT COUNT(*) FROM processed_zone.dim_client"),
            "fact": _scalar(database, "SELECT COUNT(*) FROM processed_zone.fact_mailer"),
            "fact_waves": [w for (w,) in _rows(
                database, "SELECT DISTINCT wave FROM processed_zone.fact_mailer ORDER BY wave")],
            # A fingerprint of every column of every row, so "nothing changed" is a statement
            # about values and not only about counts.
            "raw_checksum": _scalar(database, "SELECT CHECKSUM_AGG(CHECKSUM(*)) "
                                              "FROM raw_zone.mail_offers"),
            "fact_checksum": _scalar(database, "SELECT CHECKSUM_AGG(CHECKSUM(*)) "
                                               "FROM processed_zone.fact_mailer"),
            "staging_rows": _scalar(database, "SELECT (SELECT COUNT(*) FROM "
                                              "raw_zone.tmp_mail_offers) + (SELECT COUNT(*) "
                                              "FROM raw_zone.tmp_client_attributes)"),
        })
    return {"database": database, "runs": runs}


def test_the_four_runs_land_4974_then_20996_then_32198_then_nothing(chain):
    """The extract half, measured at the landing blob the loader reads, with the watermark."""
    assert [run["landed"] for run in chain["runs"]] == EXPECTED_LANDED
    assert [run["watermark"] for run in chain["runs"]] == EXPECTED_WATERMARKS


def test_the_warehouse_grows_a_wave_at_a_time_and_run_four_adds_nothing(chain):
    """raw_zone.mail_offers and fact_mailer reach 58,168 on run 3 and stay there on run 4.

    This is acceptance criterion 3 carried through the load: a loader that ignored the watermark
    would be handed all 58,168 rows again on run 4, and the MERGE would hide it -- the counts
    would still read 58,168. What separates the two is the landing count above (0, not 58,168)
    together with the fact that the counts here did not move.
    """
    runs = chain["runs"]
    assert [run["raw_mail_offers"] for run in runs] == EXPECTED_RAW_MAIL_OFFERS
    assert [run["fact"] for run in runs] == EXPECTED_FACT
    assert [run["fact_waves"] for run in runs] == [[1], [1, 2], [1, 2, 3], [1, 2, 3]]


def test_run_four_changes_no_value_in_the_warehouse(chain):
    """Not only the counts: every column of every row of raw_zone and the fact is as run 3 left it.

    The processed layer re-stages wave 3 on run 4 (its own watermark is ``>=``) and MERGEs all
    32,198 rows onto the values they already hold. The key makes that a no-op; this proves it.
    """
    third, fourth = chain["runs"][2], chain["runs"][3]
    assert fourth["raw_checksum"] == third["raw_checksum"]
    assert fourth["fact_checksum"] == third["fact_checksum"]


def test_client_attributes_reloads_whole_on_every_run_and_never_earns_a_watermark(chain):
    """58,168 rows into raw_zone and dim_client on all four runs; no watermark property, ever."""
    for run in chain["runs"]:
        assert run["raw_client_attributes"] == SOURCE_ROW_COUNT
        assert run["dim_client"] == SOURCE_ROW_COUNT
        assert "last_extracted_value" not in run["client_watermark"]
        assert "load_column" not in run["client_watermark"]


def test_every_load_leaves_the_staging_tables_empty(chain):
    """The post-MERGE TRUNCATE ran and committed with the load, on every run."""
    assert [run["staging_rows"] for run in chain["runs"]] == [0, 0, 0, 0]


def test_wave_one_treatment_nulls_reach_the_fact_as_nulls(chain):
    """Fourteen flags NULL on every wave-1 row, 69,636 cells, in raw_zone AND in fact_mailer.

    The rule the SMALLINT columns exist for, re-earned on an engine where ``CAST('' AS SMALLINT)``
    is 0. If the loader let an empty CSV field reach the server as text, these would be zeros and
    every count would still add up.
    """
    database = chain["database"]
    for table in ("raw_zone.mail_offers", "processed_zone.fact_mailer"):
        nulls = _rows(database, "SELECT {0} FROM {1} WHERE wave = 1".format(
            ", ".join("SUM(CASE WHEN {0} IS NULL THEN 1 ELSE 0 END)".format(flag)
                      for flag in TREATMENT_FLAGS), table))[0]
        assert sum(1 for count in nulls if count == WAVE_ONE_ROWS) == WAVE_ONE_WHOLLY_NULL_FLAGS
        assert sum(nulls) == WAVE_ONE_NULL_CELLS
    # bad_account: non-null exactly where a loan was taken, and never coalesced.
    assert _scalar(database, "SELECT COUNT(bad_account) FROM processed_zone.fact_mailer") == 4381
    assert _scalar(database, "SELECT COUNT(*) FROM processed_zone.fact_mailer "
                             "WHERE took_up = 1") == 4381


def test_dim_client_flags_are_nullable_bit_and_keep_null(chain):
    """``(ca.female = 1)`` rewritten as a CASE with no ELSE: 1 -> 1, other -> 0, NULL -> NULL."""
    database = chain["database"]
    types = dict(_rows(database, "SELECT column_name, data_type FROM information_schema.columns "
                                 "WHERE table_schema = 'processed_zone' "
                                 "AND table_name = 'dim_client'"))
    assert types["is_female"] == "bit" and types["is_more_educated"] == "bit"
    for flag, raw in (("is_female", "female"), ("is_more_educated", "edhi")):
        dim = _rows(database, "SELECT SUM(CASE WHEN {0} = 1 THEN 1 ELSE 0 END), "
                              "SUM(CASE WHEN {0} = 0 THEN 1 ELSE 0 END), "
                              "SUM(CASE WHEN {0} IS NULL THEN 1 ELSE 0 END) "
                              "FROM processed_zone.dim_client".format(flag))[0]
        source = _rows(database, "SELECT SUM(CASE WHEN {0} = 1 THEN 1 ELSE 0 END), "
                                 "SUM(CASE WHEN {0} <> 1 THEN 1 ELSE 0 END), "
                                 "SUM(CASE WHEN {0} IS NULL THEN 1 ELSE 0 END) "
                                 "FROM raw_zone.client_attributes".format(raw))[0]
        assert dim == source, "%s: dim %s, raw %s" % (flag, dim, source)
        assert sum(dim) == SOURCE_ROW_COUNT


def test_the_posterior_matches_the_published_table(chain):
    """All 18 posterior means after wave 3, to the five places the AWS README prints them."""
    rows = _rows(chain["database"],
                 "SELECT risk_band, arm_index, posterior_mean FROM processed_zone.bandit_posterior "
                 "WHERE run_id = 'through-wave-3' AND through_wave = 3 ORDER BY arm_id")
    measured = {}
    for band, index, mean in rows:
        measured.setdefault(band, []).append(str(mean.quantize(Decimal("0.00001"))))
    assert dict((band, tuple(means)) for band, means in measured.items()) == PUBLISHED_POSTERIOR


# --------------------------------------------------------------------------------------------
# PARITY WITH THE AWS PIPELINE, RUN UNMODIFIED ON ITS OWN DUCKDB WAREHOUSE
# --------------------------------------------------------------------------------------------

def _aws(script, *arguments, cwd):
    """Run one AWS job as the AWS README runs it: a subprocess with --local and explicit paths."""
    code = subprocess.call([sys.executable, os.path.join(AWS_PROJECT, *script.split("/"))]
                           + list(arguments), cwd=cwd)
    assert code == 0, "AWS %s failed" % script


@pytest.fixture(scope="module")
def duckdb_star(builder, source_tables, tmp_path_factory):
    """The AWS pipeline's own four-run sequence and refinery, from its README, on DuckDB.

    Every path is passed explicitly and the working directory is a temporary one, so nothing is
    written inside the AWS project.
    """
    root = tmp_path_factory.mktemp("aws")
    source, warehouse = str(root / "source.duckdb"), str(root / "warehouse.duckdb")
    config, landing = str(root / "watermark.json"), str(root / "landing")
    cwd = str(root)

    _aws("local-development/apply_ddl.py", "--db", warehouse, "--drop", cwd=cwd)
    _aws("dynamodb/write-to-dynamo.py", "--local", "--path", config, cwd=cwd)
    built = None
    for through_wave in SOURCE_THROUGH_WAVE:
        if through_wave != built:
            builder.write_duckdb(source, builder.stage(source_tables, through_wave))
            built = through_wave
        for table, load_type in (("mail_offers", "incremental"), ("client_attributes",
                                                                  "full_load")):
            _aws("glue-jobs/mysql-extraction.py", "--local", "--table_name", table,
                 "--load_type", load_type, "--source-db", source, "--config-file", config,
                 "--output-dir", landing, cwd=cwd)
            _aws("glue-jobs/redshift-raw-ingestion.py", "--local", "--table_name", table,
                 "--local-db", warehouse, "--local-landing", landing, cwd=cwd)
        _aws("glue-jobs/redshift-processed-layer.py", "--local", "--local-db", warehouse, cwd=cwd)
    _aws("glue-jobs/glue-refinery-path3.py", "--local", "--local-db", warehouse, cwd=cwd)
    return warehouse


def _duckdb_rows(warehouse, sql):
    import duckdb

    conn = duckdb.connect(warehouse, read_only=True)
    try:
        return [tuple(row) for row in conn.execute(sql).fetchall()]
    finally:
        conn.close()


# One query per table, sent verbatim to both engines. ORDER BY the natural key, so the comparison
# is row for row; SELECT * so a column one engine has and the other lacks fails the comparison.
STAR_QUERIES = [
    "SELECT * FROM raw_zone.mail_offers ORDER BY client_id, wave",
    "SELECT * FROM raw_zone.client_attributes ORDER BY client_id",
    "SELECT * FROM processed_zone.dim_client ORDER BY client_id",
    "SELECT * FROM processed_zone.fact_mailer ORDER BY client_id, wave",
    "SELECT * FROM processed_zone.dim_offer_arm ORDER BY arm_id",
]
BANDIT_QUERIES = [
    "SELECT * FROM processed_zone.bandit_posterior WHERE run_id = 'through-wave-3' "
    "ORDER BY through_wave, arm_id",
    "SELECT * FROM processed_zone.bandit_policy_value WHERE run_id = 'through-wave-3' "
    "ORDER BY eval_wave, risk_band",
]


def test_the_star_matches_the_one_the_aws_jobs_build(chain, duckdb_star):
    """Both raw tables, the dimension, the fact and the arm grid, value for value.

    232,690 rows across five tables. SMALLINT against SMALLINT, DECIMAL(5,2) against
    DECIMAL(5,2) as Decimal on both sides, and DuckDB's BOOLEAN against SQL Server's BIT, both of
    which the drivers return as True / False / None -- so the CASE-with-no-ELSE rewrite is being
    compared with Redshift's ``(ca.female = 1)`` on every client, including the NULLs.
    """
    for sql in STAR_QUERIES:
        azure = _rows(chain["database"], sql)
        aws = _duckdb_rows(duckdb_star, sql)
        assert len(azure) == len(aws) and len(azure) > 0, sql
        mismatched = [i for i, (a, b) in enumerate(zip(azure, aws)) if a != b]
        assert not mismatched, "%s: %s rows differ, first %r vs %r" % (
            sql, len(mismatched), azure[mismatched[0]], aws[mismatched[0]])


def test_the_bandit_tables_match_the_ones_the_aws_job_writes(chain, duckdb_star):
    """bandit_posterior and bandit_policy_value for through-wave-3, to the eighth decimal.

    Including the Monte-Carlo columns: same seed, same draws, same replicates and the same
    consumption order give the same stream, so ts_probability, the SNIPS value and its bootstrap
    interval are expected to agree exactly, not approximately. The DECIMAL(10,8) literals are
    written at the declared scale by the same function, so both engines store the same digits.
    """
    for sql in BANDIT_QUERIES:
        azure = _rows(chain["database"], sql)
        aws = _duckdb_rows(duckdb_star, sql)
        assert len(azure) == len(aws) and len(azure) > 0, sql
        assert azure == aws, sql


# --------------------------------------------------------------------------------------------
# WHAT SQL SERVER ENFORCES THAT REDSHIFT DID NOT -- one scratch database, emptied per test
# --------------------------------------------------------------------------------------------

MAIL_OFFERS = list(ingestion.TABLES["mail_offers"]["columns"])
CLIENT_ATTRIBUTES = list(ingestion.TABLES["client_attributes"]["columns"])


@pytest.fixture(scope="module")
def loads(scratch):
    return {"database": scratch.database(), "container": scratch.container()}


@pytest.fixture
def empty(loads):
    """Every raw_zone table emptied and committed before the test."""
    conn = _sql(loads["database"])
    try:
        cursor = conn.cursor()
        for table in ("mail_offers", "client_attributes", "tmp_mail_offers",
                      "tmp_client_attributes"):
            cursor.execute("TRUNCATE TABLE raw_zone.%s" % table)
        conn.commit()
    finally:
        conn.close()
    return loads


def _land(container, table, rows, header=None):
    """Write a landing blob the way the extractor would: header line, then the rows."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header or ingestion.TABLES[table]["columns"])
    writer.writerows(rows)
    az.blob_container(container).upload_blob(az.landing_blob_name(table),
                                             buffer.getvalue().encode("utf-8"), overwrite=True)


def _client(client_id, race="black", risk="HIGH", female="1", edhi="0"):
    return [str(client_id), race, risk, female, edhi, "12", "3"]


def _mailer(client_id, wave, **values):
    row = dict((column, "0") for column in MAIL_OFFERS)
    row.update({"client_id": str(client_id), "wave": str(wave), "offer4": "9.70"})
    row.update(values)
    return [row[column] for column in MAIL_OFFERS]


def _ingest(loads, table):
    _run_main(ingestion, ["--table_name", table, "--container", loads["container"],
                          "--database", loads["database"]])


def test_varchar16_headroom_is_enforced(empty):
    """16 characters load; 17 fail the whole load and leave the table as it was.

    The DDL sizes ``race`` and ``risk`` at 16 against measured maxima of 8 and 6, and says so.
    DuckDB accepted the declaration without checking it, so on the AWS side the headroom was a
    stated decision. Here both sides of the boundary are loaded.
    """
    import pyodbc

    sixteen = "x" * 16
    _land(empty["container"], "client_attributes", [_client(1, race=sixteen)])
    _ingest(empty, "client_attributes")
    assert _rows(empty["database"], "SELECT race FROM raw_zone.client_attributes") == \
        [(sixteen,)]

    _land(empty["container"], "client_attributes", [_client(1), _client(2, race="y" * 17)])
    with pytest.raises(pyodbc.Error) as caught:
        _ingest(empty, "client_attributes")
    assert "truncat" in str(caught.value).lower()
    # Client 1 was NOT updated to 'black' and client 2 was not inserted: the load is one unit.
    assert _rows(empty["database"], "SELECT client_id, race FROM raw_zone.client_attributes") == \
        [(1, sixteen)]

    # That refusal came from pyodbc, which sizes the fast path's parameter buffer from the width
    # the server reports for the column. The server enforces the same width on its own: an
    # ordinary parameterised INSERT of 17 characters is error 2628, not a truncated row.
    conn = _sql(empty["database"])
    try:
        with pytest.raises(pyodbc.Error) as caught:
            conn.cursor().execute("INSERT INTO raw_zone.client_attributes (client_id, race) "
                                  "VALUES (?, ?)", 2, "y" * 17)
        assert "2628" in str(caught.value)
    finally:
        conn.rollback()
        conn.close()


def test_a_duplicated_key_fails_the_load_where_redshift_would_keep_both(empty):
    """PRIMARY KEY is enforced: the second insert of (7, 2) is error 2627, and nothing lands.

    The staging table has no key -- SELECT ... INTO does not copy constraints, as CTAS does not on
    Redshift -- so both copies reach the MERGE, and it is the target's key that refuses.
    """
    import pyodbc

    _land(empty["container"], "mail_offers", [_mailer(6, 2), _mailer(7, 2), _mailer(7, 2)])
    with pytest.raises(pyodbc.IntegrityError) as caught:
        _ingest(empty, "mail_offers")
    assert "2627" in str(caught.value)
    assert _scalar(empty["database"], "SELECT COUNT(*) FROM raw_zone.mail_offers") == 0


def test_truncate_rolls_back_with_a_failed_load(empty):
    """The load is ONE transaction here. On Redshift it is three, split at the two TRUNCATEs.

    A sentinel row is committed into the staging table first. The load then truncates staging,
    inserts, and fails at the MERGE. If TRUNCATE committed on its own, as Redshift's does, the
    sentinel would be gone; on SQL Server the rollback restores it, with the target untouched.
    """
    conn = _sql(empty["database"])
    try:
        conn.cursor().execute("INSERT INTO raw_zone.tmp_mail_offers (client_id, wave) "
                              "VALUES (999999, 9)")
        conn.commit()
    finally:
        conn.close()

    _land(empty["container"], "mail_offers", [_mailer(8, 3), _mailer(8, 3)])
    with pytest.raises(Exception):
        _ingest(empty, "mail_offers")

    assert _rows(empty["database"], "SELECT client_id, wave FROM raw_zone.tmp_mail_offers") == \
        [(999999, 9)]
    assert _scalar(empty["database"], "SELECT COUNT(*) FROM raw_zone.mail_offers") == 0


def test_an_empty_field_lands_as_null_and_not_as_zero(empty):
    """EMPTYASNULL, applied by the loader because T-SQL would turn '' into 0 for a SMALLINT."""
    assert _scalar(empty["database"], "SELECT CAST('' AS SMALLINT)") == 0   # the trap, measured
    _land(empty["container"], "client_attributes", [_client(3, race="", female="", edhi="0")])
    _land(empty["container"], "mail_offers", [_mailer(3, 1, prize="", badacct_last="")])
    _ingest(empty, "client_attributes")
    _ingest(empty, "mail_offers")
    assert _rows(empty["database"], "SELECT race, female, edhi FROM raw_zone.client_attributes") \
        == [(None, None, 0)]
    assert _rows(empty["database"], "SELECT prize, badacct_last, intshown FROM "
                                    "raw_zone.mail_offers") == [(None, None, 0)]


def test_reloading_the_same_extract_changes_nothing(empty):
    """The MERGE makes a re-run idempotent: same rows, same values, no duplicates."""
    _land(empty["container"], "mail_offers", [_mailer(4, 1), _mailer(5, 2, prize="1")])
    _ingest(empty, "mail_offers")
    first = _rows(empty["database"], "SELECT * FROM raw_zone.mail_offers ORDER BY client_id")
    _ingest(empty, "mail_offers")
    assert _rows(empty["database"], "SELECT * FROM raw_zone.mail_offers ORDER BY client_id") == \
        first
    assert len(first) == 2


def test_a_reordered_extract_is_refused_before_the_warehouse_is_touched(empty):
    """The header is checked against the column list. A positional COPY could not do this."""
    swapped = list(CLIENT_ATTRIBUTES)
    swapped[3], swapped[4] = swapped[4], swapped[3]                  # female <-> edhi
    _land(empty["container"], "client_attributes", [_client(9)], header=swapped)
    with pytest.raises(ValueError):
        _ingest(empty, "client_attributes")
    assert _scalar(empty["database"], "SELECT COUNT(*) FROM raw_zone.client_attributes") == 0


# --------------------------------------------------------------------------------------------
# THE DDL, THE WATERMARK TABLE AND THE CHAIN
# --------------------------------------------------------------------------------------------

def test_the_ddl_is_go_batches_with_no_create_database_and_applies_atomically(scratch, tmp_path):
    """Five batches, CREATE SCHEMA opening two of them, and a failure rolls every batch back.

    A deliberately broken last batch is appended to the real DDL. T-SQL DDL is transactional, so
    the rollback takes the schemas and tables of the first five batches with it: the database is
    left with no tables rather than half a schema.
    """
    with open(apply_ddl.DEFAULT_DDL, encoding="utf-8") as handle:
        text = handle.read()
    parts = apply_ddl.batches(text)
    assert len(parts) == 5
    code = [[line for line in part.splitlines()
             if line.strip() and not line.strip().startswith("--")] for part in parts]
    # In the statements, that is -- the header comment explains why the line is absent.
    assert not [line for lines in code for line in lines if "create database" in line.lower()]
    assert [lines for lines in code if any(l.startswith("CREATE SCHEMA") for l in lines)] == \
        [["CREATE SCHEMA raw_zone;"], ["CREATE SCHEMA processed_zone;"]], \
        "CREATE SCHEMA must be alone in its batch"

    broken = tmp_path / "broken.sql"
    broken.write_text(text + "\nGO\nSELECT * FROM processed_zone.no_such_table;\n",
                      encoding="utf-8")
    name = "cm_test_atomic_" + os.urandom(4).hex()
    scratch.databases.append(name)
    with pytest.raises(Exception):
        apply_ddl.apply(name, str(broken), drop=True)
    assert _scalar(name, "SELECT COUNT(*) FROM information_schema.tables") == 0


def test_a_merge_mode_reset_keeps_the_watermark_and_replace_clears_it(scratch):
    """Why the seeder writes with REPLACE: a None is not stored, so MERGE cannot clear anything."""
    from azure.data.tables import UpdateMode

    table = scratch.config_table()
    client = az.config_table(table)
    _, _, etag = extraction.fetch_configuration(client, "mail_offers")
    extraction.update_last_extracted_value(client, "mail_offers", "3", etag)

    client.upsert_entity(dict(seeder.entity(seeder.CONFIGURATIONS[0]),
                              last_extracted_value=None), mode=UpdateMode.MERGE)
    assert client.get_entity(az.CONFIG_PARTITION, "mail_offers")["last_extracted_value"] == "3"

    seeder.write(table)
    assert "last_extracted_value" not in client.get_entity(az.CONFIG_PARTITION, "mail_offers")


def test_the_watermark_write_refuses_a_stale_read(scratch):
    """The watermark write is conditional on the ETag it read, so a mid-run reset is not undone."""
    from azure.core.exceptions import ResourceModifiedError

    table = scratch.config_table()
    client = az.config_table(table)
    _, _, etag = extraction.fetch_configuration(client, "mail_offers")
    seeder.write(table)                                   # someone resets while the extract runs
    with pytest.raises(ResourceModifiedError):
        extraction.update_last_extracted_value(client, "mail_offers", "1", etag)
    assert "last_extracted_value" not in client.get_entity(az.CONFIG_PARTITION, "mail_offers")


def test_the_chain_is_the_state_machine_in_the_same_order():
    """Six states with the Step Functions definition's names, order and job arguments."""
    with open(os.path.join(AWS_PROJECT, "step-functions", "step-functions.json"),
              encoding="utf-8") as handle:
        definition = json.load(handle)
    states, name = [], definition["StartAt"]
    while name:
        state = definition["States"][name]
        arguments = state["Parameters"].get("Arguments", {})
        states.append((name, [part for key in ("--table_name", "--load_type") if key in arguments
                              for part in (key, arguments[key])]))
        name = state.get("Next")
    assert [(name, args) for name, _, args in chain_runner.STATES] == states


def test_a_failing_state_stops_the_chain(scratch, builder, source_tables, tmp_path):
    """A disagreement between chain and config exits 1 in the first state, and nothing runs after.

    mail_offers is given no load_column, so ExtractMailOffers -- which asks for an incremental
    load -- exits 1 before touching the source. The runner must return that code and never start
    RawIngestMailOffers: the landing container stays empty and the database is never loaded.
    """
    source = str(tmp_path / "source.duckdb")
    builder.write_duckdb(source, builder.stage(source_tables, 1))
    table, container, database = scratch.config_table(), scratch.container(), scratch.database()
    from azure.data.tables import UpdateMode

    # REPLACE, explicitly: upsert_entity defaults to MERGE, which would leave load_column in place.
    az.config_table(table).upsert_entity({"PartitionKey": az.CONFIG_PARTITION,
                                          "RowKey": "mail_offers"}, mode=UpdateMode.REPLACE)
    code = subprocess.call([sys.executable, os.path.join(PROJECT_ROOT, "orchestration",
                                                         "run-chain.py"),
                            "--source-db", source, "--database", database,
                            "--config-table", table, "--container", container])
    assert code == 1
    assert list(az.blob_container(container).list_blobs()) == []
    assert _scalar(database, "SELECT COUNT(*) FROM raw_zone.mail_offers") == 0


def test_the_warehouse_jobs_own_self_checks_pass():
    """Each job's --self-check, which includes the AWS job's own assertions it reuses."""
    az.self_check()
    ingestion.self_check()
    processed.self_check()
    refinery.self_check()
