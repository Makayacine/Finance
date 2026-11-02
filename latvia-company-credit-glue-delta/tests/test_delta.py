"""The MERGE job against real Delta tables in a temporary folder: restatement, replay, time travel,
duplicate keys, the curation, and the batches it must refuse."""

from datetime import date

import pandas as pd
import pytest

import lakehouse as lh
from company_credit import config, replay
from conftest import load_job

merge_job = load_job("glue-delta-merge")
LAST = pd.Timestamp("2026-11-01")


def put(bucket, source, day, frame):
    """Write one batch file the way stage_batches.py does."""
    path = bucket.root / lh.RAW / source / f"{source}_{day}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, sep=";", index=False)


def stage(bucket, source, batches):
    for day, rows in batches.groupby("batch_date"):
        put(bucket, source, pd.Timestamp(day).date().isoformat(), rows.drop(columns="batch_date"))


def proceeding(pid, regcode, started, entered, ended=None, code=None):
    return {"proceeding_id": pid, "debtor_registration_number": regcode,
            "proceeding_started_on": started, "proceeding_ended_on": ended,
            "proceeding_form": "INSOLVENCY", "proceeding_type": "NATIONAL",
            "proceeding_resolution_type": code, "entry_created_on": entered}


def read(spark, bucket, table, version=None):
    reader = spark.read.format("delta")
    if version is not None:
        reader = reader.option("versionAsOf", version)
    return reader.load(lh.table_path(bucket, table)).toPandas()


def test_a_proceeding_arrives_open_and_the_merge_restates_it(spark, bucket):
    source = "insolvency_legal_person_proceeding"
    batches = replay.proceeding_batches(pd.DataFrame([
        proceeding("1", "40003000001", "2014-02-03", "2014-02-05 10:00:00", "2014-11-20", "M08"),
        proceeding("2", "40003000002", "2014-05-06", "2014-05-07 09:30:00"),
    ]), LAST)
    stage(bucket, source, batches)
    log = merge_job.run(spark, bucket, "proceedings")

    assert [(e["batch_date"], e["inserted"], e["restated"]) for e in log] == [
        ("2014-03-31", 1, 0), ("2014-06-30", 1, 0), ("2014-12-31", 0, 1)]
    now = read(spark, bucket, "proceedings").set_index("proceeding_id")
    assert now.loc["1", "proceeding_resolution_type"] == "M08"
    assert str(now.loc["1", "proceeding_ended_on"]) == "2014-11-20"
    first = read(spark, bucket, "proceedings", version=0).set_index("proceeding_id")
    assert pd.isna(first.loc["1", "proceeding_resolution_type"])
    assert bucket.list(lh.RAW) == []
    assert len(bucket.list(f"{lh.ARCHIVED}/{source}")) == 3


def test_an_older_batch_replayed_late_changes_nothing(spark, bucket):
    source = "insolvency_legal_person_proceeding"
    batches = replay.proceeding_batches(pd.DataFrame([
        proceeding("1", "40003000001", "2014-02-03", "2014-02-05 10:00:00", "2014-11-20", "M08"),
    ]), LAST)
    stage(bucket, source, batches)
    merge_job.run(spark, bucket, "proceedings")
    # The open version of proceeding 1 lands again, under a later batch date, after its close.
    put(bucket, source, "2015-03-31", batches[batches.batch_date == "2014-03-31"]
        .drop(columns="batch_date"))
    log = merge_job.run(spark, bucket, "proceedings")
    assert (log[0]["inserted"], log[0]["restated"], log[0]["unchanged"]) == (0, 0, 1)
    assert read(spark, bucket, "proceedings").proceeding_resolution_type.tolist() == ["M08"]


def company(regcode, registered, terminated=None):
    return {"regcode": regcode, "type": "SIA", "registered": registered,
            "terminated": terminated, "closed": "L" if terminated else None}


def test_a_later_batch_cannot_change_an_earlier_as_of_view(spark, bucket):
    register = pd.DataFrame([
        company("A", "2013-01-10", "2014-05-20"),
        company("B", "2013-02-11", "2014-08-15"),
        company("C", "2014-04-02"),
    ])
    batches = replay.register_batches(register, LAST)
    stage(bucket, "register", batches[batches.batch_date <= "2014-06-30"])
    merge_job.run(spark, bucket, "companies")

    path = lh.table_path(bucket, "companies")
    versions = lh.batch_versions(spark, path)
    view = lh.read_as_of(spark, path, versions, date(2014, 3, 31)).toPandas()
    assert sorted(view.regcode) == ["A", "B"] and view.terminated.isna().all()

    stage(bucket, "register", batches[batches.batch_date > "2014-06-30"])
    merge_job.run(spark, bucket, "companies")
    versions = lh.batch_versions(spark, path)
    again = lh.read_as_of(spark, path, versions, date(2014, 3, 31)).toPandas()
    pd.testing.assert_frame_equal(view.sort_values("regcode").reset_index(drop=True),
                                  again.sort_values("regcode").reset_index(drop=True))
    june = lh.read_as_of(spark, path, versions, date(2014, 6, 30)).toPandas().set_index("regcode")
    assert june.terminated.notna().to_dict() == {"A": True, "B": False, "C": False}
    latest = read(spark, bucket, "companies").set_index("regcode")
    assert latest.terminated.notna().to_dict() == {"A": True, "B": True, "C": False}
    assert lh.read_as_of(spark, path, versions, date(2008, 1, 1)) is None


def statement(sid, regcode, year, ended, created, currency="EUR", rounding="ONES",
              source_type="UGP", schema="DokGPv1", started=None):
    return {"id": sid, "file_id": sid, "legal_entity_registration_number": regcode,
            "source_schema": schema, "source_type": source_type, "year": year,
            "year_started_on": started or f"{year}-01-01", "year_ended_on": ended,
            "employees": 3, "rounded_to_nearest": rounding, "currency": currency,
            "created_at": created}


def balance(sid, equity, assets):
    row = {c: 0 for c in config.BALANCE_COLUMNS}
    row.update(statement_id=sid, equity=equity, total_assets=assets)
    return row


def income(sid, net_income):
    row = {c: 0 for c in config.INCOME_COLUMNS}
    row.update(statement_id=sid, net_income=net_income)
    return row


def stage_statements(bucket, rows, balances, incomes):
    fs, bs, inc = replay.statement_batches(
        pd.DataFrame(rows).astype(str), pd.DataFrame(balances).astype(str),
        pd.DataFrame(incomes).astype(str), LAST)
    for source, frame in (("financial_statements", fs), ("balance_sheets", bs),
                          ("income_statements", inc)):
        stage(bucket, source, frame)


def test_curation_converts_lats_scales_thousands_and_drops_group_accounts(spark, bucket):
    stage_statements(bucket, [
        statement(1, "A", 2012, "2012-12-31", "2020-09-01 00:00:00", currency="LVL"),
        statement(2, "B", 2012, "2012-12-31", "2020-09-02 00:00:00", rounding="THOUSANDS"),
        statement(3, "B", 2012, "2012-12-31", "2020-09-02 00:00:01", source_type="UKGP"),
    ], [balance(1, 702804, 1405608), balance(2, 5, 10), balance(3, 99, 99)],
        [income(1, 70280), income(2, 1), income(3, 9)])
    log = merge_job.run(spark, bucket, "statements")
    assert log[0]["batch_date"] == "2013-09-30" and log[0]["consolidated_dropped"] == 1
    frame = read(spark, bucket, "statements").set_index("regcode")
    assert frame.loc["A", "equity"] == pytest.approx(1_000_000.0)
    assert frame.loc["A", "net_income"] == pytest.approx(70280 / config.LVL_PER_EUR)
    assert frame.loc["B", "total_assets"] == 10_000.0 and frame.loc["B", "statement_id"] == 2
    assert str(frame.loc["A", "available_on"]) == "2013-09-30" and frame.loc["A", "bulk_dated"]


def test_available_on_in_spark_matches_the_replay_rule(spark, bucket):
    cases = [("2019-02-28", "2020-09-30 11:00:00"), ("2020-05-31", "2020-10-01 00:00:00"),
             ("2020-06-30", "2020-12-10 09:00:00.5"), ("2023-12-31", "2024-05-02 23:59:59.12")]
    rows = [statement(i, f"R{i}", int(ended[:4]), ended, created) for i, (ended, created)
            in enumerate(cases, start=1)]
    stage_statements(bucket, rows, [balance(i, 1, 2) for i in range(1, 5)],
                     [income(i, 0) for i in range(1, 5)])
    merge_job.run(spark, bucket, "statements")
    spark_dates = read(spark, bucket, "statements").set_index("statement_id").available_on
    expected = replay.available_on([c for _, c in cases], [e for e, _ in cases])
    assert [spark_dates[i] for i in range(1, 5)] == list(expected.dt.date)


def test_two_statements_for_one_year_in_one_batch_keep_the_winner(spark, bucket):
    stage_statements(bucket, [statement(1, "A", 2022, "2022-12-31", "2023-03-01 10:00:00")],
                     [balance(1, 1, 10)], [income(1, 0)])
    merge_job.run(spark, bucket, "statements")
    # Next quarter: a refiling of 2022 and, for the same year, a short period ending later.
    stage_statements(bucket, [
        statement(2, "A", 2022, "2022-12-31", "2023-05-01 10:00:00"),
        statement(3, "A", 2022, "2023-02-28", "2023-05-02 10:00:00", started="2023-01-01"),
    ], [balance(2, 2, 10), balance(3, 3, 10)], [income(2, 0), income(3, 0)])
    log = merge_job.run(spark, bucket, "statements")
    assert (log[0]["duplicates_dropped"], log[0]["restated"]) == (1, 1)
    assert read(spark, bucket, "statements").statement_id.tolist() == [3]


def test_a_bare_merge_refuses_two_source_rows_for_one_key(spark, bucket):
    from delta.tables import DeltaTable

    path = lh.table_path(bucket, "bare")
    spark.createDataFrame([("A", 2022, 1)], "regcode string, fiscal_year int, sid int") \
        .write.format("delta").save(path)
    twice = spark.createDataFrame([("A", 2022, 2), ("A", 2022, 3)],
                                  "regcode string, fiscal_year int, sid int")
    with pytest.raises(Exception, match="MULTIPLE_SOURCE_ROW|multiple source rows"):
        (DeltaTable.forPath(spark, path).alias("t")
         .merge(twice.alias("s"), "t.regcode = s.regcode AND t.fiscal_year = s.fiscal_year")
         .whenMatchedUpdateAll().whenNotMatchedInsertAll().execute())


def test_a_statement_batch_missing_a_file_is_left_in_raw_zone(spark, bucket):
    stage_statements(bucket, [statement(1, "A", 2022, "2022-12-31", "2023-03-01 10:00:00")],
                     [balance(1, 1, 10)], [income(1, 0)])
    (bucket.root / lh.RAW / "income_statements" / "income_statements_2023-03-31.csv").unlink()
    with pytest.raises(FileNotFoundError, match="income_statements_2023-03-31.csv missing"):
        merge_job.run(spark, bucket, "statements")
    assert len(bucket.list(lh.RAW)) == 2
    assert not lh.is_delta(spark, lh.table_path(bucket, "statements"))


def test_a_file_without_a_batch_name_stops_the_job(spark, bucket):
    stray = bucket.root / lh.RAW / "register" / "register.csv"
    stray.parent.mkdir(parents=True)
    stray.write_text("regcode;type;registered;terminated;closed\n", encoding="utf-8")
    with pytest.raises(ValueError, match="not a register_<YYYY-MM-DD>.csv batch file"):
        merge_job.run(spark, bucket, "companies")
