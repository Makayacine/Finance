"""Weight of evidence: fine classing on the training years, WoE and IV per bin, and the WoE values
a characteristic is replaced by before the logistic regression.

    WoE(bin) = ln( share of non-defaults in the bin / share of defaults in the bin )   low = risky
    IV       = sum over bins of (share of non-defaults - share of defaults) * WoE

A numeric characteristic is cut at its training deciles, on the rows where it is a number; the
states that are not a number (``none``, ``zero base``) are bins of their own. A bin holding under
``min_share`` of the training rows is folded into its neighbour -- the next decile for a numeric
characteristic, ``other`` for a category -- because a cell with a few dozen companies and no
default is a WoE of plus infinity held back only by the smoothing. The edges and the WoE are
frozen at fit time and applied unchanged to every later year, which is what lets PSI compare
them.
"""

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

SMOOTHING = 0.5


def woe_table(bins, y):
    frame = pd.DataFrame({"bin": np.asarray(bins, dtype=object), "y": np.asarray(y, dtype=float)})
    table = frame.groupby("bin", sort=False).y.agg(n="size", events="sum")
    table["non_events"] = table.n - table.events
    events = (table.events + SMOOTHING) / (table.events.sum() + SMOOTHING)
    non_events = (table.non_events + SMOOTHING) / (table.non_events.sum() + SMOOTHING)
    table["event_rate"] = table.events / table.n
    table["woe"] = np.log(non_events / events)
    table["iv"] = (non_events - events) * table.woe
    return table


@dataclass
class Binning:
    name: str
    numeric: bool
    edges: list = field(default_factory=list)  # interior cut points, numeric only
    categories: list = field(default_factory=list)  # kept categories, categorical only
    woe: dict = field(default_factory=dict)
    table: pd.DataFrame = None

    @property
    def iv(self):
        return float(self.table.iv.sum())

    def bins(self, values, states=None):
        if not self.numeric:
            values = pd.Series(values, dtype=object).fillna("none").astype(str)
            return values.where(values.isin(self.categories), "other").to_numpy(dtype=object)
        values = np.asarray(values, dtype=float)
        index = np.searchsorted(np.asarray(self.edges, dtype=float), values, side="right")
        labels = np.array([self._label(i) for i in range(len(self.edges) + 1)], dtype=object)
        out = labels[np.clip(index, 0, len(self.edges))]
        if states is not None:
            states = np.asarray(states, dtype=object)
            out = np.where(states == "value", out, states)
        out = np.where(pd.isna(values) & (out == labels[-1]), "none", out)
        return out

    def _label(self, i):
        low = "-inf" if i == 0 else f"{self.edges[i - 1]:.6g}"
        high = "inf" if i == len(self.edges) else f"{self.edges[i]:.6g}"
        return f"[{low}, {high})"

    def transform(self, values, states=None):
        """WoE of each row's bin; a bin never seen in training gets WoE 0, the average."""
        return pd.Series(self.bins(values, states)).map(self.woe).fillna(0.0).to_numpy()


def fit_numeric(name, values, y, states=None, n_bins=10, min_share=0.005):
    values = np.asarray(values, dtype=float)
    finite = values[np.isfinite(values) & ((np.asarray(states) == "value") if states is not None
                                            else True)]
    quantiles = np.quantile(finite, np.linspace(0, 1, n_bins + 1)[1:-1]) if len(finite) else []
    edges = sorted(set(float(q) for q in quantiles))
    binning = Binning(name=name, numeric=True, edges=edges)
    # Fold any bin under min_share into its right-hand neighbour (the last into its left), then
    # re-count, until every value bin carries its share.
    while binning.edges:
        counts = pd.Series(binning.bins(finite)).value_counts()
        labels = [binning._label(i) for i in range(len(binning.edges) + 1)]
        small = [i for i, label in enumerate(labels) if counts.get(label, 0) < min_share * len(values)]
        if not small:
            break
        i = small[0]
        del binning.edges[min(i, len(binning.edges) - 1)]
    return _finish(binning, binning.bins(values, states), y)


def fit_categorical(name, values, y, min_share=0.005):
    values = pd.Series(values, dtype=object).fillna("none").astype(str)
    shares = values.value_counts(normalize=True)
    binning = Binning(name=name, numeric=False, categories=sorted(shares[shares >= min_share].index))
    return _finish(binning, binning.bins(values), y)


def _finish(binning, bins, y):
    table = woe_table(bins, y)
    binning.table = table.sort_values("woe")
    binning.woe = table.woe.to_dict()
    return binning
