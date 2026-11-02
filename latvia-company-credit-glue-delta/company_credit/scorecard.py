"""The PD scorecard: WoE-coded characteristics, a logistic regression, two levels.

Each characteristic is replaced by the WoE of its bin and enters the regression as one term, so
it carries one coefficient and one clustered p-value. With the default as the event, a
characteristic that ranks the way its WoE says has a coefficient near -1; a positive one means a
neighbour has taken its information. Selection is the course's, and runs on the training years
only: drop a characteristic with IV under 0.02, then refit and drop the worst of any with the
wrong sign or a p-value over 0.05, one at a time, until none is left.

**One characteristic skips the IV screen.** ``accounts_lag`` -- whether the latest accounts are
for the last fiscal year or the one before, i.e. whether the company filed late -- has an IV of
about 0.016 on 2009-17 and 0.096 on 2021-25. The difference is the bulk load: in the training
years a statement filed two years late is dated as if it had been filed on time, so late filers
mostly look like on-time ones and the signal is diluted. It is kept by design (``FORCED``) and
must still pass the sign and p-value checks; the README reports the model without it beside.

**Two levels.** ``ttc`` is the model as fitted, through the cycle of its training years, 2009-17.
``validation`` shifts its log-odds by one constant so that its mean PD on the validation years,
2018-20, equals their observed default rate. The shift changes no ranking, so AUC is the same
under both; only calibration differs, and both are reported year by year.
"""

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import features as feat
from . import logit, woe

MIN_IV, MAX_P = 0.02, 0.05
FORCED = ("accounts_lag",)
MIN_SCORE, MAX_SCORE = 300, 850


@dataclass
class Scorecard:
    binnings: dict
    selected: list
    result: logit.LogitResult = None
    shift: float = 0.0
    dropped: list = field(default_factory=list)

    def woe_columns(self, frame, names=None):
        names = self.selected if names is None else names
        columns = {"const": np.ones(len(frame))}
        for name in names:
            binning = self.binnings[name]
            states = frame.get(name + "_state") if binning.numeric else None
            columns[name] = binning.transform(frame[name], None if states is None else states.values)
        return pd.DataFrame(columns, index=frame.index)

    def linear(self, frame):
        return self.result.linear(self.woe_columns(frame).to_numpy())

    def pd(self, frame, level="ttc"):
        shift = self.shift if level == "validation" else 0.0
        return logit.sigmoid(self.linear(frame) + shift)

    def calibrate(self, frame, y="y"):
        """Set the validation level from these rows' observed default rate."""
        self.shift = logit.intercept_shift(self.linear(frame), float(frame[y].mean()))
        return self.shift

    def iv_table(self):
        return pd.DataFrame(
            [(name, b.iv, len(b.table), name in self.selected) for name, b in self.binnings.items()],
            columns=["characteristic", "iv", "bins", "in_model"],
        ).sort_values("iv", ascending=False).reset_index(drop=True)

    def points(self):
        """The course's min-max scaling of each bin's contribution to a 300-850 score."""
        params = dict(zip(self.result.names, self.result.params))
        rows = []
        for name in self.selected:
            for bin_label, value in self.binnings[name].woe.items():
                rows.append((name, bin_label, -params[name] * value))
        table = pd.DataFrame(rows, columns=["characteristic", "bin", "goodness"])
        intercept = -params["const"]
        low = intercept + table.groupby("characteristic").goodness.min().sum()
        high = intercept + table.groupby("characteristic").goodness.max().sum()
        factor = (MAX_SCORE - MIN_SCORE) / (high - low)
        table["points"] = (table.goodness * factor).round()
        table.attrs["intercept_points"] = round((intercept - low) * factor + MIN_SCORE)
        return table.drop(columns="goodness")


def fit(train, characteristics=feat.FEATURES, y="y", groups="regcode", forced=FORCED):
    binnings = {}
    for name in characteristics:
        if name in feat.CATEGORICAL:
            binnings[name] = woe.fit_categorical(name, train[name], train[y])
        else:
            binnings[name] = woe.fit_numeric(name, train[name], train[y],
                                             states=train[name + "_state"].values)
    selected = [n for n in characteristics if binnings[n].iv >= MIN_IV or n in forced]
    card = Scorecard(binnings=binnings, selected=selected,
                     dropped=[(n, f"IV {binnings[n].iv:.4f}") for n in characteristics
                              if n not in selected])
    while True:
        X = card.woe_columns(train)
        card.result = logit.fit(X.to_numpy(), train[y].to_numpy(), groups=train[groups].to_numpy(),
                                names=list(X.columns))
        table = card.result.table().set_index("term").drop(index="const")
        bad = table[(table.coef > 0) | (table.p_value > MAX_P)]
        if bad.empty:
            return card
        worst = bad.p_value.idxmax() if (bad.coef <= 0).all() else bad.coef.idxmax()
        reason = "wrong sign" if table.loc[worst, "coef"] > 0 else f"p {table.loc[worst, 'p_value']:.3f}"
        card.selected.remove(worst)
        card.dropped.append((worst, reason))
