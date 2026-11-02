"""The label rule and the embargo, on invented companies whose answer is known by hand."""

from datetime import date

import pandas as pd

from company_credit import config, panel, replay

OBS_YEAR = 2014  # observation 2014-10-01, horizon to 2015-10-01


def company(regcode, registered="2005-01-01", terminated=None, kind="SIA"):
    return {"regcode": regcode, "type": kind, "registered": registered, "terminated": terminated}


def proceeding(pid, regcode, started, entered=None, ended=None, form="INSOLVENCY", code=None):
    return {"proceeding_id": pid, "regcode": regcode, "proceeding_started_on": started,
            "proceeding_ended_on": ended, "proceeding_form": form,
            "proceeding_resolution_type": code,
            "entry_created_on": entered or started}


def as_known(proceedings, day):
    """The insolvency register as it stood on ``day``: entered by then, resolved only if ended."""
    frame = pd.DataFrame(proceedings)
    frame = frame[pd.to_datetime(frame.entry_created_on) <= pd.Timestamp(day)].copy()
    open_then = pd.to_datetime(frame.proceeding_ended_on) > pd.Timestamp(day)
    frame.loc[open_then, ["proceeding_ended_on", "proceeding_resolution_type"]] = None
    return frame


COMPANIES = pd.DataFrame([
    company("A"),                                   # nothing happens
    company("B"),                                   # insolvency inside the horizon
    company("C"),                                   # application dismissed inside the horizon
    company("D"),                                   # already in default before the observation
    company("E"),                                   # dismissed earlier, real default now
    company("F"),                                   # legal protection inside, insolvency after
    company("G", terminated="2014-06-30"),          # terminated before the observation
    company("H", registered="2014-11-01"),          # registered after it
    company("I"),                                   # started before, entered after the obs date
    company("J"),                                   # default after the horizon ends
    company("K", terminated="2015-03-31"),          # struck off inside the horizon, no proceeding
    company("L"),                                   # application undecided at the horizon's end
])
PROCEEDINGS = [
    proceeding(1, "B", "2015-02-10", ended="2016-08-01", code="M09"),
    proceeding(2, "C", "2015-01-05", ended="2015-02-15", code="M01"),
    proceeding(3, "D", "2013-05-01", ended="2014-12-01", code="M08"),
    proceeding(4, "E", "2012-03-01", ended="2012-04-10", code="M01"),
    proceeding(5, "E", "2015-06-01"),
    proceeding(6, "F", "2015-04-01", ended="2015-07-01", form="LEGAL_PROTECTION", code="T14"),
    proceeding(7, "F", "2015-11-01"),
    proceeding(8, "I", "2014-09-26", entered="2014-10-03"),
    proceeding(9, "J", "2015-10-02"),
    proceeding(10, "L", "2015-09-20", ended="2015-11-02", code="M01"),
]


def rows():
    obs, end = panel.observation_date(OBS_YEAR), panel.horizon_end(OBS_YEAR)
    statements = pd.DataFrame(columns=["regcode", "statement_id", "year_ended_on", "created_at",
                                       "available_on"])
    return panel.observation_rows(
        COMPANIES, statements, as_known(PROCEEDINGS, panel.eve(obs)),
        as_known(PROCEEDINGS, panel.eve(end)), OBS_YEAR,
    ).set_index("regcode")


def test_population_drops_dead_unborn_and_already_defaulted():
    assert set(rows().index) == {"A", "B", "C", "E", "F", "I", "J", "K", "L"}


def test_each_label_by_hand():
    y = rows().y.to_dict()
    assert y == {"A": 0, "B": 1, "C": 0, "E": 1, "F": 1, "I": 1, "J": 0, "K": 0, "L": 1}


def test_insolvency_only_label_leaves_out_legal_protection():
    frame = rows()
    assert frame.loc["F", "y"] == 1 and frame.loc["F", "y_insolvency"] == 0
    assert frame.loc["B", "y_insolvency"] == 1


def test_dismissal_counts_only_once_known():
    # C's application was dismissed inside the horizon, so it is not a default; L's was still
    # undecided on the eve of the horizon's end, so it is one, as it stood then.
    frame = rows()
    assert frame.loc["C", "y"] == 0 and frame.loc["L", "y"] == 1


def test_labelled_years_stop_at_the_as_of_date():
    assert panel.labelled_years(date(2026, 10, 30)) == list(range(2009, 2026))
    assert panel.labelled_years(date(2026, 9, 30))[-1] == 2024


def statement(sid, ended, created):
    return {"regcode": "A", "statement_id": sid, "year_ended_on": ended, "created_at": created}


def test_embargo_dates_bulk_loaded_statements_nine_months_after_the_period():
    frame = pd.DataFrame([
        statement(1, "2012-12-31", "2020-09-01 10:00:00"),      # bulk load: assumed date
        statement(2, "2020-06-30", "2020-12-10 09:00:00.5"),    # bulk window, stamp is earlier
        statement(3, "2022-12-31", "2023-05-11 08:00:00.123"),  # real filing time
    ])
    available = replay.available_on(frame.created_at, frame.year_ended_on)
    assert list(available.dt.date) == [date(2013, 9, 30), date(2020, 12, 10), date(2023, 5, 11)]
    assert config.BULK_LOAD_END == date(2021, 1, 18)


def test_accounts_are_the_latest_available_and_at_most_24_months_old():
    frame = pd.DataFrame([
        statement(1, "2020-12-31", "2021-05-01 00:00:00"),
        statement(2, "2021-12-31", "2022-05-02 00:00:00"),
        statement(3, "2022-12-31", "2023-10-05 00:00:00"),   # filed after 1 October: too late
    ])
    frame["available_on"] = replay.available_on(frame.created_at, frame.year_ended_on)
    chosen = panel.latest_statements(frame, date(2023, 10, 1))
    assert list(chosen.statement_id) == [2]
    assert panel.accounts_lag(chosen.year_ended_on, date(2023, 10, 1)).tolist() == ["previous"]
    # Two years on, statement 2 is 33 months old and the company is not covered.
    assert panel.latest_statements(frame[frame.statement_id < 3], date(2024, 10, 1)).empty
