"""Basel IRB capital for a corporate exposure, and the IFRS 9 stage-2 rule this book can support.

THE IRB FORMULA, AND WHAT GOES INTO ITS SLOTS
---------------------------------------------

    R = 0.12 * (1 - e^(-50 PD)) / (1 - e^(-50)) + 0.24 * (1 - (1 - e^(-50 PD)) / (1 - e^(-50)))
    b = (0.11852 - 0.05478 * ln PD)^2
    K = [LGD * N( (G(PD) + sqrt(R) * G(0.999)) / sqrt(1 - R) ) - PD * LGD]
        * (1 + (M - 2.5) * b) / (1 - 1.5 * b)
    RWA = 12.5 * K * EAD

That is the Basel corporate curve without the SME firm-size adjustment, which needs annual sales
the FOIA file does not carry. The adjustment would lower R by up to 0.04 for the smallest firms,
so leaving it out gives these small businesses the capital of a large corporate.

What fills each slot is stated where it is filled, because none of them is the Basel quantity:

*   **PD** is the 12-month probability of SBA *charge-off* from the hazard curve. Charge-off
    comes after the lender has liquidated, which is months or years after the 90-days-past-due
    point Basel calls default, and a default that recovers in full never charges off at all. It
    is floored at 0.05%, the Basel III corporate floor.
*   **LGD** is the predicted *gross charge-off severity*, charged-off amount over original
    amount, conditional on charge-off, gross of recoveries, capped at 1. It is not discounted and
    carries no downturn add-on.
*   **EAD** is the original approved amount. The charged-off amount can only bound the balance at
    default from below, so the original is the one exposure the file identifies, and it is an
    upper bound.
*   **M** is 2.5 years, the foundation-approach default; a term the file would supply is the
    field the charge-off process rewrites.

IFRS 9 STAGING ON A FILE WITH NO PAYMENT HISTORY
------------------------------------------------

Stage 2 means a significant increase in credit risk since origination. The FOIA file hides every
open loan behind EXEMPT, so it holds no days-past-due, no modification flag and no rating
history: for one loan, nothing it shows has changed since the day it was approved. What does
arrive after origination is the experience of the loan's own cohort. By the as-of date every
vintage has charged off some number of loans, and the calibrated hazard says how many it should
have, given each loan's band and how long it was open. IFRS 9 allows the increase to be assessed
collectively on shared risk characteristics when loan-level evidence is missing (B5.5.1-B5.5.6),
so the rule is:

    a vintage whose observed charge-offs are at least STAGE2_RELATIVE times the expected,
    with a one-sided Poisson p-value below STAGE2_P_VALUE, puts its open loans in stage 2.

A score band cannot do this job on its own. It is fixed at origination, so a high-risk band is
high risk from the first day and has not increased since. Stage 3 needs an observed default, and
this file shows none for an open loan, so it is empty by construction, not by finding.
"""

import numpy as np
from scipy.stats import norm

PD_FLOOR = 0.0005
MATURITY_YEARS = 2.5
CONFIDENCE = 0.999
STAGE2_RELATIVE = 2.0
STAGE2_P_VALUE = 0.01


def irb_correlation(pd_):
    weight = (1 - np.exp(-50 * pd_)) / (1 - np.exp(-50))
    return 0.12 * weight + 0.24 * (1 - weight)


def irb_capital(pd_, lgd, maturity=MATURITY_YEARS):
    """Capital requirement K per unit of EAD."""
    pd_ = np.clip(np.asarray(pd_, dtype=float), PD_FLOOR, 0.9999)
    lgd = np.asarray(lgd, dtype=float)
    r = irb_correlation(pd_)
    b = (0.11852 - 0.05478 * np.log(pd_)) ** 2
    conditional = norm.cdf((norm.ppf(pd_) + np.sqrt(r) * norm.ppf(CONFIDENCE)) / np.sqrt(1 - r))
    return (lgd * conditional - pd_ * lgd) * (1 + (maturity - 2.5) * b) / (1 - 1.5 * b)


def risk_weighted_assets(pd_, lgd, ead, maturity=MATURITY_YEARS):
    return 12.5 * irb_capital(pd_, lgd, maturity) * np.asarray(ead, dtype=float)


def stage2_vintages(experience):
    """The vintages whose charge-offs ran at least STAGE2_RELATIVE times the expected."""
    hit = (experience.observed_over_expected >= STAGE2_RELATIVE) & (
        experience.p_value < STAGE2_P_VALUE)
    return sorted(int(v) for v in experience.index[hit])
