"""WoE, the hand-written logit and the metrics, each against a value worked out another way."""

import math

import numpy as np
import pandas as pd
import pytest

from company_credit import logit, metrics, woe


def test_woe_and_iv_by_hand():
    # Bin a: 10 defaults, 90 good; bin b: 30 defaults, 70 good. With the 0.5 smoothing.
    bins = ["a"] * 100 + ["b"] * 100
    y = [1] * 10 + [0] * 90 + [1] * 30 + [0] * 70
    table = woe.woe_table(bins, y)
    events = {"a": 10.5 / 40.5, "b": 30.5 / 40.5}
    goods = {"a": 90.5 / 160.5, "b": 70.5 / 160.5}
    for b in ("a", "b"):
        assert table.loc[b, "woe"] == pytest.approx(math.log(goods[b] / events[b]))
    assert table.iv.sum() == pytest.approx(sum((goods[b] - events[b]) * math.log(goods[b] / events[b])
                                               for b in ("a", "b")))
    assert table.loc["a", "woe"] > 0 > table.loc["b", "woe"]  # low WoE is the risky bin


def test_numeric_binning_keeps_special_states_apart_and_folds_rare_bins():
    rng = np.random.default_rng(0)
    values = np.concatenate([rng.normal(size=1000), [np.nan] * 50])
    states = np.array(["value"] * 1000 + ["zero base"] * 50, dtype=object)
    y = rng.integers(0, 2, size=1050)
    binning = woe.fit_numeric("x", values, y, states=states, n_bins=10, min_share=0.2)
    labels = set(binning.bins(values, states))
    assert "zero base" in labels
    counts = pd.Series(binning.bins(values[:1000], states[:1000])).value_counts()
    assert counts.min() >= 0.2 * 1050  # every value bin carries its share after folding
    assert binning.transform(np.array([np.nan]), np.array(["never seen"]))[0] == 0.0


def test_auc_counts_a_tie_as_half():
    assert metrics.auc([1, 0], [1.0, 1.0]) == 0.5
    assert metrics.auc([1, 1, 0, 0], [0.9, 0.4, 0.4, 0.1]) == pytest.approx(0.875)


def test_delong_interval_matches_a_stratified_bootstrap():
    rng = np.random.default_rng(1)
    y = np.r_[np.ones(300), np.zeros(3000)].astype(bool)
    score = np.r_[rng.normal(1.0, 1, 300), rng.normal(0.0, 1, 3000)]
    value, low, high = metrics.delong(y, score)
    assert value == pytest.approx(metrics.auc(y, score))
    pos, neg = np.flatnonzero(y), np.flatnonzero(~y)
    draws = []
    for _ in range(400):
        idx = np.r_[rng.choice(pos, len(pos)), rng.choice(neg, len(neg))]
        draws.append(metrics.auc(y[idx], score[idx]))
    assert (high - low) / (2 * metrics.Z95) == pytest.approx(np.std(draws), rel=0.15)


def test_ks_and_psi():
    assert metrics.ks([1, 1, 0, 0], [0.9, 0.8, 0.2, 0.1]) == 1.0
    reference = np.arange(1000.0)
    edges = metrics.decile_edges(reference)
    assert metrics.psi(reference, reference, edges) == pytest.approx(0.0)
    assert metrics.psi(reference, reference + 300, edges) > 0.25


def test_logit_and_clustered_errors_agree_with_statsmodels():
    sm = pytest.importorskip("statsmodels.api")
    rng = np.random.default_rng(2)
    n, firms = 5000, 800
    groups = rng.integers(0, firms, size=n)
    shock = rng.normal(0, 0.8, size=firms)[groups]
    X = np.column_stack([np.ones(n), rng.normal(size=n), rng.normal(size=n)])
    y = (rng.random(n) < logit.sigmoid(-2 + 0.7 * X[:, 1] - 0.4 * X[:, 2] + shock)).astype(float)
    ours = logit.fit(X, y, groups=groups)
    theirs = sm.Logit(y, X).fit(disp=0, cov_type="cluster", cov_kwds={"groups": groups})
    np.testing.assert_allclose(ours.params, theirs.params, rtol=1e-8)
    np.testing.assert_allclose(ours.bse, theirs.bse, rtol=1e-6)
    np.testing.assert_allclose(ours.pvalues, theirs.pvalues, rtol=1e-5, atol=1e-12)


def test_intercept_shift_hits_the_target_rate_and_keeps_the_ranking():
    eta = np.linspace(-6, -2, 1000)
    shift = logit.intercept_shift(eta, 0.002)
    assert logit.sigmoid(eta + shift).mean() == pytest.approx(0.002, rel=1e-9)
    assert np.all(np.diff(logit.sigmoid(eta + shift)) > 0)
