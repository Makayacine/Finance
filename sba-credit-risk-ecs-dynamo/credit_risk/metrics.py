"""Discrimination, stability and calibration measures, written out rather than imported.

AUC is the Mann-Whitney statistic on average ranks, so tied scores count half -- which matters
here, because a WoE scorecard gives every loan in the same cell of every characteristic exactly
the same score and ties are the norm, not the exception. Gini is ``2 * AUC - 1``. KS is the
largest gap between the cumulative score distributions of events and non-events, evaluated only
at distinct score values so a tie cannot split.

The bootstrap resamples events and non-events separately. With 77 failures in the FDIC test
window an unstratified resample would sometimes draw 60 and sometimes 95, and the interval would
then be measuring the event count as much as the ranking.

PSI is the course's population stability index on shared bins: under 0.10 stable, 0.10 to 0.25 a
shift worth a look, above 0.25 a population the model was not built on.
"""

import numpy as np
import pandas as pd
from scipy.stats import rankdata


def auc(y, score):
    y = np.asarray(y, dtype=bool)
    ranks = rankdata(np.asarray(score, dtype=float))
    n1, n0 = y.sum(), (~y).sum()
    if n1 == 0 or n0 == 0:
        return float("nan")
    return float((ranks[y].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


def ks(y, score):
    y = np.asarray(y, dtype=bool)
    score = np.asarray(score, dtype=float)
    values, inverse = np.unique(score, return_inverse=True)
    events = np.bincount(inverse, weights=y, minlength=len(values))
    totals = np.bincount(inverse, minlength=len(values))
    cum_events = np.cumsum(events) / max(events.sum(), 1)
    cum_non = np.cumsum(totals - events) / max((totals - events).sum(), 1)
    return float(np.max(np.abs(cum_events - cum_non)))


def discrimination(y, score, n_boot=500, seed=0):
    """AUC, Gini and KS with stratified-bootstrap 95% intervals on AUC and Gini."""
    y = np.asarray(y, dtype=bool)
    score = np.asarray(score, dtype=float)
    point = auc(y, score)
    rng = np.random.default_rng(seed)
    events, non_events = np.flatnonzero(y), np.flatnonzero(~y)
    draws = []
    if len(events) and len(non_events):
        for _ in range(n_boot):
            idx = np.concatenate(
                [rng.choice(events, len(events)), rng.choice(non_events, len(non_events))]
            )
            draws.append(auc(y[idx], score[idx]))
    low, high = np.percentile(draws, [2.5, 97.5]) if draws else (np.nan, np.nan)
    return {
        "n": int(len(y)),
        "events": int(y.sum()),
        "event_rate": float(y.mean()) if len(y) else float("nan"),
        "auc": point,
        "auc_low": float(low),
        "auc_high": float(high),
        "gini": 2 * point - 1,
        "gini_low": float(2 * low - 1),
        "gini_high": float(2 * high - 1),
        "ks": ks(y, score) if len(events) and len(non_events) else float("nan"),
    }


def psi(expected, actual, edges):
    """Population stability index of ``actual`` against ``expected`` on fixed bin edges."""
    e = np.histogram(np.asarray(expected, dtype=float), bins=edges)[0].astype(float)
    a = np.histogram(np.asarray(actual, dtype=float), bins=edges)[0].astype(float)
    e = np.clip(e / e.sum(), 1e-6, None)
    a = np.clip(a / a.sum(), 1e-6, None)
    return float(np.sum((a - e) * np.log(a / e)))


def psi_categorical(expected, actual):
    """PSI over the categories of a binned characteristic."""
    e = pd.Series(expected).value_counts(normalize=True)
    a = pd.Series(actual).value_counts(normalize=True)
    cats = e.index.union(a.index)
    e = e.reindex(cats, fill_value=0).clip(lower=1e-6)
    a = a.reindex(cats, fill_value=0).clip(lower=1e-6)
    return float(((a - e) * np.log(a / e)).sum())


def calibration_table(y, predicted, edges):
    """Observed against predicted event rate inside fixed predicted-PD bands."""
    frame = pd.DataFrame({"y": np.asarray(y, dtype=float), "p": np.asarray(predicted, float)})
    frame["band"] = pd.cut(frame.p, edges, right=False)
    table = frame.groupby("band", observed=False).agg(
        n=("y", "size"), events=("y", "sum"), predicted=("p", "mean"), observed=("y", "mean")
    )
    table["expected_events"] = frame.groupby("band", observed=False).p.sum()
    return table.reset_index()
