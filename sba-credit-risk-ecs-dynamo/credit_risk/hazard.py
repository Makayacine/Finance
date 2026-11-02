"""Monthly charge-off hazard by loan age, with payoff as the competing exit.

The FOIA file carries no default date, only a charge-off date, and only for loans that were
charged off. That is enough for a life table. Every loan enters at age 0 on its first
disbursement -- or, when the table is left-truncated, at its age on the day observation starts --
and leaves by one of three doors:

    CHGOFF    leaves on ChargeOffDate       the event
    P I F     leaves on PaidInFullDate      the competing exit: a repaid loan cannot charge off
    EXEMPT    still open on the as-of date  censored there -- at risk until then, never a "good"

For each month of age ``a``, ``h_event[a]`` is the charge-offs in that month over the loans still
open at its start, and ``h_competing[a]`` the same for payoffs. Survival is the product of
``1 - h_event - h_competing`` over the months before; the cumulative incidence of charge-off is
the sum of survival times event hazard. That sum is the quantity both regimes want:

    Basel 12-month PD at age a     incidence over [a, a + 12), given open at a
    IFRS 9 lifetime PD at age a    incidence over [a, the end of the curve), given open at a

Treating payoff as competing and not as censoring is not a refinement. Censoring would assume a
repaid loan goes on carrying the hazard of the loans that did not repay, and the lifetime PD would
then count charge-offs from loans that no longer exist. From origination, the company book's
lifetime PD is 15.8% with payoff competing and 49.1% with payoff censored.

**The contractual term is not needed, and is not used.** A loan's scheduled maturity shows up in
the payoff hazard, so running the incidence to the end of the curve gives the remaining-life
PD of the book as it actually amortised and prepaid. ``TermInMonths`` is rewritten for
charged-off loans, and nothing here reads it.

**Dates are labels.** Disbursement, charge-off and paid-in-full dates decide when a loan left
and why. None of them is ever a model feature, and the scorecard in ``sba.py`` cannot see them.

**The tail is cut, not extrapolated.** Past the age where fewer than ``min_at_risk`` loans remain
open, one charge-off moves the hazard by whole percentage points. The curve stops there and the
hazard beyond it is zero, so a lifetime PD is the incidence up to that age.

**Band calibration is a proportional-hazards assumption.** ``band_multipliers`` divides a band's
observed charge-offs by the charge-offs the pooled curve expected over that band's exposure,
month by month. A band's hazard is then the pooled hazard times that ratio at every age. The
shape is shared and only the level moves; the notebook overlays the bands' own empirical curves
to show how far that holds.
"""

from dataclasses import dataclass

import numpy as np

DAYS_PER_MONTH = 30.4375


def age_in_months(start, end):
    """Whole months between two datetime Series, floored at 0."""
    return np.floor((end - start).dt.days / DAYS_PER_MONTH).clip(lower=0)


@dataclass
class HazardCurve:
    at_risk: np.ndarray
    events: np.ndarray
    competing: np.ndarray

    @property
    def h_event(self):
        return np.divide(self.events, self.at_risk, out=np.zeros(len(self.at_risk)),
                         where=self.at_risk > 0)

    @property
    def h_competing(self):
        return np.divide(self.competing, self.at_risk, out=np.zeros(len(self.at_risk)),
                         where=self.at_risk > 0)

    @property
    def max_age(self):
        return len(self.at_risk)

    def scaled(self, event_multiplier=1.0, competing_multiplier=1.0):
        """Hazards for one band: the pooled curve times its two multipliers, kept below 1."""
        he = self.h_event * event_multiplier
        hc = self.h_competing * competing_multiplier
        total = he + hc
        shrink = np.where(total > 1.0, 1.0 / np.maximum(total, 1e-12), 1.0)
        return he * shrink, hc * shrink

    def incidence_table(self, event_multiplier=1.0, competing_multiplier=1.0):
        """``(survival, cumulative incidence)`` from age 0, each of length max_age + 1."""
        he, hc = self.scaled(event_multiplier, competing_multiplier)
        survival = np.concatenate([[1.0], np.cumprod(1.0 - he - hc)])
        incidence = np.concatenate([[0.0], np.cumsum(survival[:-1] * he)])
        return survival, incidence

    def conditional_incidence(self, age, horizon=None, event_multiplier=1.0,
                              competing_multiplier=1.0):
        """P(charge-off within ``horizon`` months | open at ``age``); None means to the end."""
        survival, incidence = self.incidence_table(event_multiplier, competing_multiplier)
        age = np.clip(np.asarray(age, dtype=int), 0, self.max_age)
        end = self.max_age if horizon is None else np.clip(age + horizon, 0, self.max_age)
        base = survival[age]
        return np.divide(incidence[end] - incidence[age], base, out=np.zeros(len(age)),
                         where=base > 0)


def estimate(exit_age, outcome, min_at_risk=500, entry_age=None):
    """Life table from each loan's exit age and outcome (``event``/``competing``/``censored``).

    A loan that enters at age ``e`` and leaves in month ``a`` was at risk in months e..a inclusive.
    ``entry_age`` is None for a table that sees every loan from age 0; a left-truncated table
    passes the age at which each loan came under observation.
    """
    exit_age = np.asarray(exit_age, dtype=int)
    entry_age = np.zeros_like(exit_age) if entry_age is None else np.asarray(entry_age, dtype=int)
    outcome = np.asarray(outcome)
    length = int(exit_age.max()) + 1 if len(exit_age) else 1
    entered = np.bincount(entry_age, minlength=length).cumsum()
    left_before = np.concatenate([[0], np.bincount(exit_age, minlength=length).cumsum()[:-1]])
    at_risk = (entered - left_before).astype(float)
    events = np.bincount(exit_age[outcome == "event"], minlength=length).astype(float)
    competing = np.bincount(exit_age[outcome == "competing"], minlength=length).astype(float)
    keep = int(np.argmax(at_risk < min_at_risk)) if (at_risk < min_at_risk).any() else length
    return HazardCurve(at_risk[:keep], events[:keep], competing[:keep])


def cumulative_hazard(curve, exit_age, entry_age=None):
    """Nelson-Aalen expected events for each loan over the months it was at risk."""
    cum_event = np.concatenate([[0.0], np.cumsum(curve.h_event)])
    cum_comp = np.concatenate([[0.0], np.cumsum(curve.h_competing)])
    last = np.minimum(np.asarray(exit_age, dtype=int), curve.max_age - 1) + 1
    first = 0 if entry_age is None else np.minimum(np.asarray(entry_age, dtype=int), last)
    return cum_event[last] - cum_event[first], cum_comp[last] - cum_comp[first]


def band_multipliers(curve, exit_age, outcome, band, entry_age=None):
    """Observed over expected events and payoffs per band, against the pooled curve."""
    exit_age = np.asarray(exit_age, dtype=int)
    outcome = np.asarray(outcome)
    band = np.asarray(band)
    inside = exit_age < curve.max_age  # exits past the cut tail carry no curve to compare with
    expected_event, expected_comp = cumulative_hazard(curve, exit_age, entry_age)
    rows = {}
    for label in sorted(set(band[inside])):
        mask = inside & (band == label)
        rows[label] = {
            "loans": int(mask.sum()),
            "observed_events": int((outcome[mask] == "event").sum()),
            "expected_events": float(expected_event[mask].sum()),
            "observed_competing": int((outcome[mask] == "competing").sum()),
            "expected_competing": float(expected_comp[mask].sum()),
        }
    for row in rows.values():
        row["event_multiplier"] = row["observed_events"] / max(row["expected_events"], 1e-9)
        row["competing_multiplier"] = (row["observed_competing"]
                                       / max(row["expected_competing"], 1e-9))
    return rows
