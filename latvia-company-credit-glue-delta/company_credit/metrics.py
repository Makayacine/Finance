"""Discrimination, stability and calibration, written out rather than imported.

AUC is the Mann-Whitney statistic on average ranks, so tied scores count half. Gini is
``2 * AUC - 1``. KS is the largest gap between the cumulative score distributions of defaults and
non-defaults, taken only at distinct score values so a tie cannot split.

The interval on AUC is DeLong's, from each default's and each non-default's placement among the
other class. It needs one ranking where a bootstrap needs hundreds, and a test window here has
half a million company-years: 500 resamples would cost minutes per window for an interval that
agrees with DeLong's to the third decimal at these sizes.

PSI is the course's population stability index on frozen bins: under 0.10 stable, 0.10 to 0.25 a
shift worth a look, above 0.25 a population the model was not built on.
"""

import math

import numpy as np
import pandas as pd

Z95 = 1.959963984540054


def _ranks(values):
    return pd.Series(np.asarray(values, dtype=float)).rank(method="average").to_numpy()


def auc(y, score):
    y = np.asarray(y, dtype=bool)
    n1, n0 = int(y.sum()), int((~y).sum())
    if n1 == 0 or n0 == 0:
        return float("nan")
    ranks = _ranks(score)
    return float((ranks[y].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


def delong(y, score):
    """AUC with DeLong's 95% interval."""
    y = np.asarray(y, dtype=bool)
    score = np.asarray(score, dtype=float)
    n1, n0 = int(y.sum()), int((~y).sum())
    if n1 < 2 or n0 < 2:
        return float("nan"), float("nan"), float("nan")
    all_ranks = _ranks(score)
    pos_ranks = _ranks(score[y])
    neg_ranks = _ranks(score[~y])
    v10 = (all_ranks[y] - pos_ranks) / n0  # share of non-defaults each default outscores
    v01 = 1.0 - (all_ranks[~y] - neg_ranks) / n1
    value = float(v10.mean())
    se = math.sqrt(v10.var(ddof=1) / n1 + v01.var(ddof=1) / n0)
    return value, value - Z95 * se, value + Z95 * se


def ks(y, score):
    y = np.asarray(y, dtype=bool)
    values, inverse = np.unique(np.asarray(score, dtype=float), return_inverse=True)
    events = np.bincount(inverse, weights=y, minlength=len(values))
    totals = np.bincount(inverse, minlength=len(values))
    cum_events = np.cumsum(events) / max(events.sum(), 1)
    cum_non = np.cumsum(totals - events) / max((totals - events).sum(), 1)
    return float(np.max(np.abs(cum_events - cum_non)))


def discrimination(y, score):
    value, low, high = delong(y, score)
    y = np.asarray(y, dtype=bool)
    return {
        "n": int(len(y)), "defaults": int(y.sum()),
        "default_rate": float(y.mean()) if len(y) else float("nan"),
        "auc": value, "auc_low": low, "auc_high": high,
        "gini": 2 * value - 1, "ks": ks(y, score) if 0 < y.sum() < len(y) else float("nan"),
    }


def psi(expected, actual, edges):
    """Population stability index of ``actual`` against ``expected`` on fixed bin edges."""
    edges = np.asarray(edges, dtype=float)
    e = np.bincount(np.searchsorted(edges, np.asarray(expected, float), side="right"),
                    minlength=len(edges) + 1).astype(float)
    a = np.bincount(np.searchsorted(edges, np.asarray(actual, float), side="right"),
                    minlength=len(edges) + 1).astype(float)
    e = np.clip(e / e.sum(), 1e-6, None)
    a = np.clip(a / a.sum(), 1e-6, None)
    return float(np.sum((a - e) * np.log(a / e)))


def decile_edges(reference):
    return np.unique(np.quantile(np.asarray(reference, dtype=float), np.linspace(0.1, 0.9, 9)))


def calibration_by(frame, by, y="y", predicted="pd"):
    """Observed against predicted default rate, and the two counts, for each value of ``by``."""
    table = frame.groupby(by).agg(n=(y, "size"), defaults=(y, "sum"),
                                  expected=(predicted, "sum"), predicted=(predicted, "mean"),
                                  observed=(y, "mean"))
    table["observed_over_expected"] = table.defaults / table.expected
    return table.reset_index()
