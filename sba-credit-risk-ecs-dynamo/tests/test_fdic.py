"""The FDIC label rule, row by row, on five invented banks whose answers are known.

The PD panel's rule has six branches -- in the horizon, in the embargo, after failure, assisted,
censored exit, not yet observed -- and every one of them changes which rows a model is fitted on
without raising anything. The banks below are built so each branch fires on a known row:

    bank 1   fails 2010-06-15        2008 row: y = 0 (its horizon, 2009-03-31 to 2010-03-31,
                                     ends first), 2009 row: y = 1, 2010 row: after failure
    bank 2   fails 2011-02-10        2010 row: embargo (Jan-Mar), 2009 row: y = 1
    bank 3   assisted 2010-09-01     every row whose horizon reaches the assistance is gone
    bank 4   stops filing after 2009 the 2009 row is a censored exit; kept as y = 0 with "keep"
    bank 5   files every year        y = 0 except the last year, whose horizon ends after the
                                     data does (the latest event here is bank 99, mid-2013)

Plus the event table's own rules: one FAILURE per CERT with the earliest kept, ASSISTANCE never
a failure, and a run-off ratio that is never clipped at 1.
"""

import numpy as np
import pandas as pd
import pytest

from conftest import SAMPLE
from credit_risk import fdic

FIELDS = ["ASSET", "DEP", "DEPINS", "DEPUNINS", "LNLSNET", "LNLSDEPR", "EQV", "ROA", "NIMY",
          "NCLNLSR", "NTLNLSR", "RBCRWAJ"]


def yearend(certs_years):
    rows = []
    for cert, years in certs_years.items():
        for year in years:
            rows.append({"CERT": cert, "REPDTE": int(f"{year}1231"),
                         **{f: 1.0 for f in FIELDS}, "DEP": 100.0, "DEPUNINS": 20.0})
    return pd.DataFrame(rows)


def failures(rows):
    frame = pd.DataFrame(rows, columns=["CERT", "FAILDATE", "RESTYPE"])
    frame["FAILYR"] = pd.to_datetime(frame.FAILDATE, format="%m/%d/%Y").dt.year
    for column in ("QBFDEP", "QBFASSET", "COST"):
        frame[column] = 1.0
    frame["SAVR"], frame["COMMENTS"] = "DIF", np.nan
    return frame


@pytest.fixture
def toy():
    filings = yearend({1: [2007, 2008, 2009, 2010], 2: [2008, 2009, 2010],
                       3: [2007, 2008, 2009, 2010, 2011], 4: [2007, 2008, 2009],
                       5: [2009, 2010, 2011, 2012]})
    events = failures([(1, "06/15/2010", "FAILURE"), (2, "02/10/2011", "FAILURE"),
                       (3, "09/01/2010", "ASSISTANCE"), (99, "06/30/2013", "FAILURE")])
    return filings, events


def outcome(panel, cert, year):
    row = panel[(panel.CERT == cert) & (panel.year == year)].iloc[0]
    return row.drop_reason, int(row.label)


def test_each_branch_of_the_label_rule(toy):
    panel = fdic.pd_panel(*toy)
    assert outcome(panel, 1, 2008) == ("kept", 0)
    assert outcome(panel, 1, 2009) == ("kept", 1)
    assert outcome(panel, 1, 2010)[0] == "after failure"
    assert outcome(panel, 2, 2009) == ("kept", 1)
    assert outcome(panel, 2, 2010)[0] == "embargo"
    assert outcome(panel, 3, 2008)[0] == "kept"  # horizon ends 2010-03-31, before assistance
    assert outcome(panel, 3, 2009)[0] == "assisted"
    assert outcome(panel, 3, 2011)[0] == "assisted"  # every row after the assistance date too
    assert outcome(panel, 4, 2008) == ("kept", 0)
    assert outcome(panel, 4, 2009)[0] == "censored exit"
    assert outcome(panel, 5, 2011) == ("kept", 0)
    assert outcome(panel, 5, 2012)[0] == "horizon not yet observed"


def test_keeping_exits_changes_only_the_censored_rows(toy):
    censor, keep = fdic.pd_panel(*toy), fdic.pd_panel(*toy, exits="keep")
    assert outcome(keep, 4, 2009) == ("kept", 0)
    changed = censor.drop_reason != keep.drop_reason
    assert set(censor.drop_reason[changed]) == {"censored exit"}
    assert outcome(keep, 5, 2012)[0] == "horizon not yet observed"


def test_one_failure_per_cert_earliest_kept_and_assistance_is_not_failure():
    events = failures([(7, "07/21/1988", "FAILURE"), (7, "02/17/1989", "FAILURE"),
                       (8, "11/23/2008", "ASSISTANCE"), (np.nan, "04/19/1934", "FAILURE")])
    found, assisted, counts = fdic.failure_events(events)
    assert list(found.CERT) == [7]
    assert found.fail_date.iloc[0] == pd.Timestamp("1988-07-21")
    assert list(assisted.CERT) == [8]
    assert counts["duplicate_failures_removed"] == 1
    assert counts["failure_rows_without_cert"] == 1


def test_sample_runoff_is_not_clipped_and_loss_flags_are_set():
    events = pd.read_csv(SAMPLE / "fdic" / "failures.csv")
    prefailure = pd.read_csv(SAMPLE / "fdic" / "financials_prefailure.csv")
    runoff, n_events = fdic.runoff_sample(events, prefailure)
    assert (runoff.runoff_ratio > 1).any()
    assert len(runoff) <= n_events
    loss = fdic.loss_sample(events, prefailure)
    assert loss.flag_systemic_2023.sum() == 3
    signature = loss[loss.CERT == 57053].iloc[0]
    assert signature.COST == 0 and signature.flag_cost_not_positive


def test_pd_coefficients_carry_bank_clustered_errors():
    panel = fdic.pd_panel(pd.read_csv(SAMPLE / "fdic" / "financials_yearend.csv"),
                          pd.read_csv(SAMPLE / "fdic" / "failures.csv"))
    rows = panel[(panel.drop_reason == "kept") & (panel.window == "train")]
    model = fdic.fit_pd(rows)
    assert model.result.cov_type == "cluster"
    assert set(model.coefficients().term) == {"const", *fdic.FEATURES}
