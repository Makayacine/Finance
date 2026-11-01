"""Weight of evidence, coarse classing, and a points scorecard on WoE-ordered dummies.

This is the course's scorecard, step for step: bin every characteristic, compute WoE and IV per
bin, coarse-class the long categorical lists by WoE, turn each binned characteristic into
dummies with its riskiest bin as the reference, fit a logistic regression and read its p-values,
then scale the coefficients to points between 300 and 850.

    WoE(bin) = ln( share of non-events in bin / share of events in bin )      low WoE = risky
    IV       = sum over bins of (share of non-events - share of events) * WoE

**The target is the event, not the good.** The course fits P(good). Here ``y = 1`` is a
charge-off, so a coefficient below zero means "safer than the reference bin", and the points
scaling flips the sign so that a higher score is still a safer loan. The p-values are unchanged
by the flip.

**A level term can ride along without scoring.** ``fit_scorecard(..., level=...)`` adds dummies
for a column whose effect is a level and not a ranking -- the SBA chain passes the approval
vintage, because FY2007 charged off at 36% and FY2003 at 15% for reasons no origination field
carries. The level dummies are fitted, reported, and then left out of the points, so the score
ranks loans within a vintage and the hazard calibration in ``sba.py`` supplies the level.

**Rare bins are folded before fitting**, into ``other``, at ``min_count`` rows. A category with a
dozen loans and no charge-off is a perfectly separated dummy, and Newton's method answers that
with a coefficient walking off to minus infinity and a p-value of 1.
"""

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import statsmodels.api as sm

SMOOTHING = 0.5  # added to each cell's event and non-event count, so an empty cell has a WoE
MIN_SCORE, MAX_SCORE = 300, 850


def woe_table(category, y):
    frame = pd.DataFrame({"bin": pd.Series(category).astype(str).values, "y": np.asarray(y)})
    table = frame.groupby("bin").y.agg(n="size", events="sum")
    table["non_events"] = table.n - table.events
    events = (table.events + SMOOTHING) / (table.events.sum() + SMOOTHING)
    non_events = (table.non_events + SMOOTHING) / (table.non_events.sum() + SMOOTHING)
    table["event_rate"] = table.events / table.n
    table["woe"] = np.log(non_events / events)
    table["iv"] = (non_events - events) * table.woe
    return table.sort_values("woe")


def fold_rare(category, min_count, other="other"):
    category = pd.Series(category).astype(str)
    counts = category.value_counts()
    rare = counts.index[counts < min_count]
    return category.where(~category.isin(rare), other), set(counts.index[counts >= min_count])


def coarse_class(category, y, groups):
    """Map each category to one of ``groups`` WoE-ordered groups of roughly equal size.

    Returns ``{category: "g1".."gK"}`` with g1 the riskiest. Categories are sorted by WoE and cut
    on cumulative row share, so a group is a run of neighbouring risk, not an arbitrary lump.
    """
    table = woe_table(category, y)
    share = table.n.cumsum() / table.n.sum()
    labels = np.minimum((share.values * groups - 1e-9).astype(int), groups - 1) + 1
    return {cat: f"g{label}" for cat, label in zip(table.index, labels)}


@dataclass
class Scorecard:
    features: list
    reference: dict
    levels: dict
    level_column: str = None
    level_reference: str = None
    level_values: list = field(default_factory=list)
    result: object = None
    points: pd.DataFrame = None
    intercept_points: float = 0.0
    columns: list = field(default_factory=list)

    def design(self, frame, with_level=True):
        parts = {"const": np.ones(len(frame))}
        for feature in self.features:
            values = frame[feature].astype(str).values
            for level in self.levels[feature]:
                parts[f"{feature}={level}"] = (values == level).astype(float)
        if with_level and self.level_column:
            values = frame[self.level_column].astype(str).values
            for level in self.level_values:
                parts[f"{self.level_column}={level}"] = (values == level).astype(float)
        return pd.DataFrame(parts, index=frame.index)

    def linear_predictor(self, frame):
        """Log-odds of the event with every level dummy at its reference."""
        X = self.design(frame, with_level=False)
        return X.values @ self.result.params[X.columns].values

    def score(self, frame):
        total = np.full(len(frame), float(self.intercept_points))
        for feature in self.features:
            lookup = self.points[self.points.feature == feature].set_index("bin").points
            total += frame[feature].astype(str).map(lookup).fillna(0.0).values
        return total

    def coefficients(self):
        table = pd.DataFrame(
            {"coef": self.result.params, "std_err": self.result.bse, "p_value": self.result.pvalues}
        )
        table.index.name = "term"
        return table.reset_index()


def fit_scorecard(frame, y, features, level=None, groups=None):
    """Logistic regression on WoE-referenced dummies; p-values clustered on ``groups`` if given."""
    reference, levels = {}, {}
    for feature in features:
        table = woe_table(frame[feature], y)
        reference[feature] = table.index[0]  # lowest WoE: the riskiest bin is the baseline
        levels[feature] = list(table.index[1:])
    card = Scorecard(features=list(features), reference=reference, levels=levels)
    if level is not None:
        values = sorted(frame[level].astype(str).unique())
        card.level_column, card.level_reference, card.level_values = level, values[0], values[1:]
    X = card.design(frame)
    card.columns = list(X.columns)
    model = sm.Logit(np.asarray(y, dtype=float), X)
    if groups is not None:
        codes = pd.factorize(pd.Series(groups).astype(str))[0]
        card.result = model.fit(
            disp=0, maxiter=200, cov_type="cluster", cov_kwds={"groups": codes}
        )
    else:
        card.result = model.fit(disp=0, maxiter=200)
    _scale_points(card)
    return card


def _scale_points(card):
    """The course's min-max scaling of coefficients to a 300-850 score, signs flipped."""
    rows = []
    for feature in card.features:
        rows.append((feature, card.reference[feature], 0.0))
        for level in card.levels[feature]:
            rows.append((feature, level, -card.result.params[f"{feature}={level}"]))
    table = pd.DataFrame(rows, columns=["feature", "bin", "goodness"])
    # As in the course, the intercept is its own group in both sums, which is what pins the
    # lowest reachable score to MIN_SCORE and the highest to MAX_SCORE.
    intercept = -card.result.params["const"]
    low = intercept + table.groupby("feature").goodness.min().sum()
    high = intercept + table.groupby("feature").goodness.max().sum()
    factor = (MAX_SCORE - MIN_SCORE) / (high - low)
    table["points"] = (table.goodness * factor).round()
    card.intercept_points = round((intercept - low) * factor + MIN_SCORE)
    card.points = table


def cutoff_table(score, y, cutoffs):
    """Approval rate and event rate among the approved, at each candidate score cut-off."""
    score = np.asarray(score, dtype=float)
    y = np.asarray(y, dtype=float)
    rows = []
    for cutoff in cutoffs:
        approved = score >= cutoff
        rows.append({
            "cutoff": cutoff,
            "approval_rate": approved.mean(),
            "event_rate_approved": y[approved].mean() if approved.any() else np.nan,
            "event_rate_rejected": y[~approved].mean() if (~approved).any() else np.nan,
            "events_avoided_share": y[~approved].sum() / max(y.sum(), 1),
        })
    return pd.DataFrame(rows)
