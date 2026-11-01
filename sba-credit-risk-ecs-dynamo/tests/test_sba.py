"""The SBA chain's decisions, each pinned on data small enough to check by hand.

What can go wrong in this chain is mostly silent. A feature that reads a label column produces a
better scorecard, not an error. A life table that censors payoffs instead of competing them
produces a higher lifetime PD that still looks like a probability. A severity rule applied in the
fit but not in the book produces a number nobody can reconcile. So the tests are about those:

*   the binning reads origination fields only -- scramble every label and every excluded field,
    including ``TermInMonths``, and the features do not move;
*   the borrower columns are never read, by the loader or into any output;
*   the life table's at-risk counts, hazards and cumulative incidence match a five-loan example
    worked out on paper, and competing risk sits below censoring;
*   the IRB formula reproduces the Basel Committee's published risk weight for PD 1%, LGD 45%;
*   the stage-2 rule, the severity rules and the scorecard's 300-850 scaling do what they say.
"""

import numpy as np
import pandas as pd
import pytest

from conftest import SAMPLE
from credit_risk import capital, hazard, metrics, sba, woe
from credit_risk.config import BORROWER_COLUMNS

LOAN_FILES = sorted((SAMPLE / "sba").glob("FOIA_7a_*.csv"))


@pytest.fixture(scope="module")
def book():
    return sba.company_book(sba.read_loans(LOAN_FILES))


def test_borrower_columns_are_never_read_and_loan_ids_are_unique():
    loans = sba.read_loans(LOAN_FILES)
    assert not set(BORROWER_COLUMNS) & set(loans.columns)
    assert loans.loan_id.is_unique


def test_company_book_keeps_companies_and_drops_cancelled_and_undisbursed(book):
    assert set(book.BusinessType) <= set(sba.COMPANY)
    assert not book.LoanStatus.isin(["CANCLD", "COMMIT"]).any()
    open_ = book[book.LoanStatus == "EXEMPT"]
    assert (open_.outcome == "censored").all()
    assert (open_.exit_date == pd.Timestamp("2026-09-30")).all()


def test_exempt_is_never_a_good_loan_in_the_scorecard_target(book):
    model = sba.fit_pd(book, n_boot=10)
    assert not book.loan_id[book.LoanStatus == "EXEMPT"].isin(model.train_ids).any()


def test_features_do_not_move_when_labels_and_excluded_fields_are_scrambled(book):
    model = sba.fit_pd(book, n_boot=10)
    scrambled = book.copy()
    rng = np.random.default_rng(0)
    for column in [*sba.LABEL_SOURCES, "disbursed", "exit_date", "outcome", "charged_off"]:
        scrambled[column] = rng.permutation(scrambled[column].values)
    for column in sba.EXCLUDED_FEATURES:
        scrambled[column] = "garbage"
    pd.testing.assert_frame_equal(model.features(book), model.features(scrambled))
    assert not set(sba.FEATURE_SOURCES) & set(sba.EXCLUDED_FEATURES)
    assert "TermInMonths" not in sba.READ_COLUMNS


def test_life_table_matches_a_hand_worked_example():
    # Five loans. Ages are the month each left in.
    #   a: charged off in month 1     b: paid off in month 1     c: open, censored at month 2
    #   d: charged off in month 2     e: paid off in month 3
    curve = hazard.estimate([1, 1, 2, 2, 3], ["event", "competing", "censored", "event",
                                              "competing"], min_at_risk=0)
    assert list(curve.at_risk) == [5, 5, 3, 1]
    assert list(curve.h_event) == [0, 0.2, 1 / 3, 0]
    assert list(curve.h_competing) == [0, 0.2, 0, 1]
    survival, incidence = curve.incidence_table()
    # S(2) = (1 - 0)(1 - 0.4) = 0.6; incidence by month 3 = 0.2 + 0.6 * 1/3 = 0.4
    assert survival[2] == pytest.approx(0.6)
    assert incidence[3] == pytest.approx(0.4)
    assert curve.conditional_incidence(np.array([2]), 1)[0] == pytest.approx(1 / 3)


def test_competing_risk_lifetime_pd_sits_below_censoring(book):
    table = book[book.in_life_table]
    curve = hazard.estimate(table.exit_age.astype(int), table.outcome, min_at_risk=20)
    competing = curve.conditional_incidence(np.zeros(1, dtype=int), None)[0]
    censored = 1 - np.prod(1 - curve.h_event)
    assert 0 < competing < censored < 1


def test_one_band_multiplier_is_exactly_one():
    exit_age = np.array([0, 1, 1, 2, 3, 3])
    outcome = np.array(["event", "competing", "event", "censored", "event", "competing"])
    curve = hazard.estimate(exit_age, outcome, min_at_risk=0)
    rows = hazard.band_multipliers(curve, exit_age, outcome, np.array(["A"] * 6))
    assert rows["A"]["event_multiplier"] == pytest.approx(1.0)
    assert rows["A"]["competing_multiplier"] == pytest.approx(1.0)


def test_irb_reproduces_the_basel_risk_weight_for_pd_one_percent():
    # BCBS's illustrative corporate risk weights: PD 1.00%, LGD 45%, M 2.5 -> 92.32%.
    k = capital.irb_capital(np.array([0.01]), np.array([0.45]))[0]
    assert 12.5 * k == pytest.approx(0.9232, abs=5e-4)
    assert capital.irb_capital(np.array([0.0]), np.array([0.45]))[0] > 0  # the PD floor


def test_stage2_needs_both_the_ratio_and_the_significance():
    experience = pd.DataFrame({"observed_over_expected": [2.5, 2.5, 1.5],
                               "p_value": [0.001, 0.2, 0.0]}, index=[2006, 2025, 2008])
    assert capital.stage2_vintages(experience) == [2006]


def test_severity_rules_differ_only_on_charge_offs_above_one(book):
    capped = sba.severity_frame(book, "cap")
    dropped = sba.severity_frame(book, "drop")
    uncapped = sba.severity_frame(book, "uncapped")
    above = (capped.severity_raw > 1).sum()
    assert len(capped) == len(uncapped) == len(dropped) + above
    assert capped.severity.max() <= 1.0
    with pytest.raises(ValueError):
        sba.severity_frame(book, "invent")


def test_scorecard_scaling_spans_300_to_850():
    rng = np.random.default_rng(1)
    frame = pd.DataFrame({"a": rng.choice(["x", "y", "z"], 3000),
                          "b": rng.choice(["p", "q"], 3000)})
    logit = -2 + (frame.a == "x") * 1.0 - (frame.b == "q") * 0.7
    y = (rng.random(3000) < 1 / (1 + np.exp(-logit))).astype(int)
    card = woe.fit_scorecard(frame, y, ["a", "b"])
    grid = pd.DataFrame([(a, b) for a in "xyz" for b in "pq"], columns=["a", "b"])
    scores = card.score(grid)
    assert scores.min() == pytest.approx(300, abs=2) and scores.max() == pytest.approx(850, abs=2)
    assert metrics.auc(y, -card.score(frame)) > 0.6


def test_woe_matches_its_definition():
    table = woe.woe_table(["a"] * 4 + ["b"] * 4, [1, 0, 0, 0, 1, 1, 1, 0])
    # a: 1 event, 3 non-events; b: 3 events, 1 non-event; smoothing 0.5 on each count
    expected_a = np.log(((3 + 0.5) / (4 + 0.5)) / ((1 + 0.5) / (4 + 0.5)))
    assert table.loc["a", "woe"] == pytest.approx(expected_a)
    assert table.index[0] == "b"  # sorted riskiest first


def test_auc_ks_and_psi_on_cases_with_known_answers():
    assert metrics.auc([0, 0, 1, 1], [0.1, 0.2, 0.8, 0.9]) == 1.0
    assert metrics.auc([0, 1, 0, 1], [0.5, 0.5, 0.5, 0.5]) == 0.5
    assert metrics.ks([0, 0, 1, 1], [0.1, 0.2, 0.8, 0.9]) == 1.0
    edges = np.array([-np.inf, 0.5, np.inf])
    assert metrics.psi([0, 1, 0, 1], [0, 1, 0, 1], edges) == pytest.approx(0.0)
