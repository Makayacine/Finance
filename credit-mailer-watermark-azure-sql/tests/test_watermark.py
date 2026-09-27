"""The four runs of the watermark demo, end to end against Azurite and the real DuckDB source.

Ported from ``credit-mailer-watermark-glue-redshift/tests/test_watermark.py``: the same nine tests,
the same numbers, the same assertions, pointed at the Azure extraction job. What was a JSON file
standing in for DynamoDB is now a Table Storage table in Azurite, and what was a directory
standing in for S3 is now a Blob container in Azurite -- reached through ``azure-data-tables`` and
``azure-storage-blob``, the clients the job itself uses. The source is unchanged: a temporary
DuckDB file built from the committed gzip by the AWS sibling's ``build_source_db.py``.

The job's own ``main()`` is called with flags on ``sys.argv``, exactly as the chain runner hands
them to it, because the ordering that makes a crash safe -- landing blob first, watermark second --
lives in ``main()`` and nowhere else.

Run 4 is still the test that matters and still the one that looks like it does nothing: the
source has not grown, the extract is empty, and the watermark must stay at '3'. A job that ignored
the stored value would pass runs 1 to 3 by luck and fail only here.

What this file does NOT cover is the other half of the chain -- the warehouse load. The AWS suite
never loaded its warehouse either; ``tests/test_warehouse.py`` is where the four runs are carried
through SQL Server.

Run from the project root, with the emulators up::

    python -m pytest tests -q
"""

import csv
import importlib.util
import io
import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JOBS = os.path.join(PROJECT_ROOT, "python-jobs")
if JOBS not in sys.path:
    sys.path.insert(0, JOBS)

import azure_common as az  # noqa: E402  (JOBS on sys.path is what makes it importable)


def load_module(relative_path, module_name):
    """Import one of the job files by path; ``mysql-extraction.py`` is not an identifier."""
    if module_name in sys.modules:
        return sys.modules[module_name]
    path = os.path.join(PROJECT_ROOT, *relative_path.split("/"))
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


extraction = load_module("python-jobs/mysql-extraction.py", "azure_mysql_extraction")

# The contract, unchanged from the AWS suite. The staging is the SOURCE growing: runs 1 to 3 see a
# source holding waves 1, 1-2 and 1-3, and run 4 sees the same source run 3 saw.
SOURCE_THROUGH_WAVE = (1, 2, 3, 3)
EXPECTED_ROWS = [4974, 20996, 32198, 0]
EXPECTED_WATERMARKS = ["1", "2", "3", "3"]
EXPECTED_WAVES = [["1"], ["2"], ["3"], []]
SOURCE_ROW_COUNT = 58168


def _extract(argv):
    """Call the job's ``main()`` the way the chain runner does: flags on ``sys.argv``."""
    saved = sys.argv
    sys.argv = ["mysql-extraction.py"] + argv
    try:
        extraction.main()
    finally:
        sys.argv = saved


def _read_landing(container, table):
    """``(header, rows, bytes)`` from the blob the run put in the landing zone."""
    data = az.blob_container(container).get_blob_client(az.landing_blob_name(table)) \
        .download_blob().readall()
    parsed = list(csv.reader(io.StringIO(data.decode("utf-8"), newline="")))
    return parsed[0], parsed[1:], len(data)


def _configuration(config_table, table):
    """One entity of the Table Storage watermark table, as a plain dict."""
    return dict(az.config_table(config_table).get_entity(az.CONFIG_PARTITION, table))


def _flags(source, config_table, container):
    return ["--source-db", source, "--config-table", config_table, "--container", container]


@pytest.fixture(scope="module")
def pipeline(scratch, builder, source_tables, tmp_path_factory):
    """Build the source, then run the four rounds in order and record what each produced.

    Module-scoped because the four runs are one sequence: run 2's result is only meaningful given
    the watermark run 1 wrote.
    """
    source = str(tmp_path_factory.mktemp("watermark") / "source.duckdb")
    config_table = scratch.config_table()
    container = scratch.container()

    built = None
    runs = []
    for through_wave in SOURCE_THROUGH_WAVE:
        if through_wave != built:
            builder.write_duckdb(source, builder.stage(source_tables, through_wave))
            built = through_wave
        _extract(["--table_name", "mail_offers", "--load_type", "incremental"]
                 + _flags(source, config_table, container))
        header, body, size = _read_landing(container, "mail_offers")
        runs.append({
            "header": header,
            "rows": len(body),
            "waves": sorted(set(row[header.index("wave")] for row in body)),
            "bytes": size,
            "watermark": _configuration(config_table, "mail_offers").get("last_extracted_value"),
        })
    return {"source": source, "config_table": config_table, "container": container,
            "runs": runs}


def test_each_run_extracts_the_wave_the_watermark_leaves_it(pipeline, builder):
    """4,974 -> 20,996 -> 32,198 -> 0. The fourth number is the one under test."""
    assert [run["rows"] for run in pipeline["runs"]] == EXPECTED_ROWS


def test_each_extract_holds_exactly_one_wave(pipeline):
    """The predicate selected a wave, rather than merely selecting the right NUMBER of rows."""
    assert [run["waves"] for run in pipeline["runs"]] == EXPECTED_WAVES


def test_the_watermark_advances_and_then_holds(pipeline):
    """1 -> 2 -> 3 -> 3, as text.

    Text because that is what the config entity stores and what the predicate compares. The type
    is asserted as well as the value: Table Storage types a property by its Python value, so an
    int here would be stored as Edm.Int32 and come back as an int, and the next run's predicate
    would compare a number where the AWS logic expects a string.
    """
    watermarks = [run["watermark"] for run in pipeline["runs"]]
    assert watermarks == EXPECTED_WATERMARKS
    assert all(isinstance(value, str) for value in watermarks)


def test_the_empty_run_lands_a_header_and_not_a_zero_byte_object(pipeline, builder):
    """Run 4's landing blob holds the 32 column names and no rows."""
    final = pipeline["runs"][-1]
    assert final["rows"] == 0
    assert final["header"] == builder.MAIL_OFFERS_COLUMNS
    assert final["bytes"] > 0


def test_every_run_lands_the_same_columns_in_the_same_order(pipeline, builder):
    """Column order is the CSV's contract with the loader, which binds by position."""
    for run in pipeline["runs"]:
        assert run["header"] == builder.MAIL_OFFERS_COLUMNS


def test_a_full_load_ignores_a_watermark_that_is_already_set(pipeline, scratch, builder):
    """``full_load`` of mail_offers with the watermark at 3 takes all 58,168 rows, and leaves it."""
    landing = scratch.container()
    assert _configuration(pipeline["config_table"], "mail_offers")["last_extracted_value"] == "3"

    _extract(["--table_name", "mail_offers", "--load_type", "full_load"]
             + _flags(pipeline["source"], pipeline["config_table"], landing))

    header, body, _ = _read_landing(landing, "mail_offers")
    assert header == builder.MAIL_OFFERS_COLUMNS
    assert len(body) == SOURCE_ROW_COUNT
    assert _configuration(pipeline["config_table"], "mail_offers")["last_extracted_value"] == "3"


def test_client_attributes_ships_every_row_and_never_earns_a_watermark(pipeline, scratch,
                                                                       builder):
    """The control table: no load column, so a full load on every run, for ever.

    In Table Storage "never earns a watermark" is an ABSENT property rather than a null one, since
    the service does not store None -- so the absence is asserted as well as the None that
    ``.get()`` turns it into.
    """
    landing = scratch.container()
    _extract(["--table_name", "client_attributes", "--load_type", "full_load"]
             + _flags(pipeline["source"], pipeline["config_table"], landing))

    header, body, _ = _read_landing(landing, "client_attributes")
    assert header == builder.CLIENT_ATTRIBUTE_COLUMNS
    assert len(body) == SOURCE_ROW_COUNT
    entity = _configuration(pipeline["config_table"], "client_attributes")
    assert entity.get("last_extracted_value") is None
    assert "last_extracted_value" not in entity and "load_column" not in entity


def test_incremental_against_a_table_with_no_load_column_exits_one(pipeline, scratch):
    """The reference lab's ``sys.exit(1)``, kept, and the landing container left untouched."""
    landing = scratch.container()
    with pytest.raises(SystemExit) as caught:
        _extract(["--table_name", "client_attributes", "--load_type", "incremental"]
                 + _flags(pipeline["source"], pipeline["config_table"], landing))
    assert caught.value.code == 1
    assert list(az.blob_container(landing).list_blobs()) == []


def test_the_jobs_own_self_check_passes():
    """``--self-check`` is the assertion set the job ships with, run here so it runs in CI."""
    extraction.self_check()
