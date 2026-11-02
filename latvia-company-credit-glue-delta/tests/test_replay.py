"""The batch builder: each row arrives once, and again only when the file shows it changed."""

from datetime import date

import pandas as pd

from company_credit import replay

LAST = pd.Timestamp("2026-11-01")


def test_a_company_arrives_live_and_is_restated_when_terminated():
    register = pd.DataFrame([
        {"regcode": "A", "type": "SIA", "registered": "2012-02-10", "terminated": "2015-08-20",
         "closed": "L"},
        {"regcode": "B", "type": "SIA", "registered": "2012-02-10", "terminated": "2012-03-01",
         "closed": "L"},
        {"regcode": "C", "type": "AS", "registered": "1995-05-05", "terminated": None,
         "closed": None},
    ])
    out = replay.register_batches(register, LAST).sort_values(["regcode", "batch_date"])
    a = out[out.regcode == "A"]
    assert list(a.batch_date.dt.date) == [date(2012, 3, 31), date(2015, 9, 30)]
    assert a.terminated.isna().tolist() == [True, False]
    assert a.closed.tolist()[0] is None and a.closed.tolist()[1] == "L"
    # Terminated inside its arrival quarter: one row, already in its final state.
    b = out[out.regcode == "B"]
    assert len(b) == 1 and b.terminated.iloc[0] == "2012-03-01"
    # Registered long before the replay starts: it arrives in the first batch.
    assert out[out.regcode == "C"].batch_date.dt.date.tolist() == [date(2008, 12, 31)]


def test_a_proceeding_arrives_open_and_is_restated_when_it_ends():
    proceedings = pd.DataFrame([{
        "proceeding_id": "1", "debtor_registration_number": "A",
        "proceeding_started_on": "2014-02-03", "proceeding_ended_on": "2016-01-15",
        "proceeding_form": "INSOLVENCY", "proceeding_type": "NATIONAL",
        "proceeding_resolution_type": "M09", "entry_created_on": "2014-02-05 10:00:00",
    }])
    out = replay.proceeding_batches(proceedings, LAST).sort_values("batch_date")
    assert list(out.batch_date.dt.date) == [date(2014, 3, 31), date(2016, 3, 31)]
    assert out.proceeding_resolution_type.isna().tolist() == [True, False]
    assert out.proceeding_ended_on.tolist()[1] == "2016-01-15"


def test_the_last_batch_is_the_newest_stamp_not_a_quarter_still_to_come():
    assert replay.batch_of(pd.Series(["2026-10-28"]), LAST).dt.date.tolist() == [date(2026, 11, 1)]


def test_statement_batches_carry_their_balance_sheet_and_income_rows():
    statements = pd.DataFrame([
        {"id": "1", "legal_entity_registration_number": "A", "year": "2012",
         "year_ended_on": "2012-12-31", "created_at": "2020-09-01 00:00:00"},
        {"id": "2", "legal_entity_registration_number": "A", "year": "2022",
         "year_ended_on": "2022-12-31", "created_at": "2023-04-25 12:00:00.1"},
    ])
    balances = pd.DataFrame([{"statement_id": "1", "equity": "5"}, {"statement_id": "2",
                                                                    "equity": "7"}])
    incomes = pd.DataFrame([{"statement_id": "2", "net_income": "1"}])
    fs, bs, inc = replay.statement_batches(statements, balances, incomes, LAST)
    assert list(fs.batch_date.dt.date) == [date(2013, 9, 30), date(2023, 6, 30)]
    assert dict(zip(bs.statement_id, bs.batch_date.dt.date)) == {"1": date(2013, 9, 30),
                                                                 "2": date(2023, 6, 30)}
    assert inc.batch_date.dt.date.tolist() == [date(2023, 6, 30)]


def test_restatement_share_counts_keys_seen_in_an_earlier_batch():
    batches = pd.DataFrame({"batch_date": pd.to_datetime(["2014-03-31", "2014-03-31",
                                                          "2014-06-30", "2014-06-30"]),
                            "proceeding_id": ["1", "2", "1", "3"]})
    table = replay.restatement_share(batches, ["proceeding_id"])
    assert table.restated.tolist() == [0, 1]
    assert table.share.tolist() == [0.0, 0.5]


def test_the_yearly_cadence_cuts_on_the_eve_of_every_observation_date():
    days = pd.Series(["2008-05-01", "2009-09-30", "2009-10-01", "2014-12-31", "2026-10-28"])
    assert replay.batch_of(days, LAST, "year").dt.date.tolist() == [
        date(2008, 12, 31), date(2009, 9, 30), date(2010, 9, 30), date(2015, 9, 30),
        date(2026, 11, 1)]
    # Both cadences have a boundary on every 30 September, so an as-of read there sees the same
    # rows: a date on or before it is in a batch on or before it under either cadence.
    for cadence in ("quarter", "year"):
        batch = replay.batch_of(pd.Series(["2013-09-30", "2013-10-01"]), LAST, cadence)
        assert batch.iloc[0] <= pd.Timestamp("2013-09-30") < batch.iloc[1]
