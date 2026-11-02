"""The SBA 7(a) chain: who is in the book, what each loan scores, and what it is expected to lose.

    read_loans()        the FOIA CSVs, minus borrower identity, plus a minted loan_id
    company_book()      corporations and partnerships, cancelled and undisbursed loans gone
    fit_pd()            WoE scorecard on origination fields, vintage as a level term
    fit_hazard()        monthly charge-off hazard by loan age, calibrated per score band
    fit_severity()      gross charge-off severity by age at charge-off, fractional logit
    live_book()         every open loan on the as-of date: PD, severity, EL, stage, capital

WHAT "DEFAULT" CAN MEAN IN THIS FILE
------------------------------------

The only default the FOIA file records is SBA charge-off. It lags Basel's 90 days past due by
the length of a liquidation, and a loan that defaults and then recovers in full never becomes a
charge-off at all. Nor is there a default date, only the charge-off date, so the scorecard's
target is **P(charge-off by the as-of date)** over a loan's whole life to 2026-09-30, not a
12-month PD. A 12-month figure comes from the hazard curve in ``hazard.py``, and it is a 12-month
probability of charge-off, which arrives later than default and less often.

**EXEMPT is censored, never good.** SBA withholds the status of every open loan, so EXEMPT holds
the performing book and the delinquent book alike. It is left out of the scorecard's target, and
counted in the hazard as at risk until the as-of date. FY2020-Present is 61% EXEMPT, so its
charge-off rate is a rate on the loans that have already finished; it is not used as a test of
the scorecard.

**Dates are labels only.** Disbursement, charge-off and paid-in-full dates decide when and how a
loan left the book. None of them reaches a feature.

**The life table starts on 2005-08-01.** No paid-in-full date in the file is earlier than
2005-05-31. 34,187 company payoffs carry that exact date and 6,671 more carry 2005-07-31, against
about 2,000 in an ordinary month, all of them FY2000-05 approvals: a date stamped when the records
were converted, not the day the loan was repaid. Left in, those loans would count as open until
2005 and thin out every early hazard. So the table is left-truncated: a loan still open on
2005-08-01 enters it at its age that day, and every exit before then is dropped, the charge-offs
with the payoffs, or the early vintages would keep their failures and lose their repayments. That
drops 46,277 loans, a quarter of FY2000-04, and makes an FY2000-04 vintage's observed against
expected its experience since August 2005 rather than over its whole life.

WHAT IS LEFT OUT OF THE FEATURES, AND WHY
-----------------------------------------

``EXCLUDED_FEATURES`` names every field that looks like an origination field and is not one.
The one that matters most is ``TermInMonths``: it is rewritten when a loan is charged off. Of
paid-in-full company loans 83.4% carry a whole-year term; of charged-off ones, 8.9%. With it in,
the scorecard's within-vintage AUC goes from about 0.65 to about 0.9, and every point of that is
the outcome leaking back. ``CollateralInd`` leaks the same way for the early vintages: of FY2000-03
approvals flagged as collateralised, 93.0% charged off against 11.3% of the rest, and of the
83,563 FY2000-03 loans paid off by 2008 just 12 carry the flag. It was attached on the way to
charge-off, not at approval. From FY2006 it is flat or protective, but a field that leaked for
FY2000-04 cannot be trusted for the vintages after, so it is out everywhere. The interest rate is
present for 0% of FY2000-04 approvals and 100% from FY2010, so it cannot rank loans across the
crisis vintages. Bank name and address are the loan's current assignee, and the secondary-market
flag is set after sale.

LGD AND EAD CANNOT BE SEPARATED, SO THE PRODUCT IS MODELLED
-----------------------------------------------------------

``GrossChargeOffAmount`` is the charged-off amount, with no balance at default and no recovery
field beside it. All the file proves is ``charged-off <= balance at default <= original``. So the
loss identity used here is

    EL = P(charge-off) * (charged-off / original) * original

where the middle term is the **gross charge-off severity**: conditional on charge-off, gross of
recoveries, on the original amount. It is neither a Basel LGD nor an LGD times a CCF, and it is
never called either.

**One rule for severity above 1.** 4,482 company charge-offs exceed their original amount, 4,076
of them on FY2000-2019 approvals -- SBA's own report defines the charged-off amount as principal
*and interest*, which is the likeliest reason, though the FOIA field is not documented that way.
They are capped at 1, which keeps the identity's bound. ``severity_sensitivity`` re-prices the
book under "drop them" and "keep them uncapped" so the cost of the rule is on the page.

**Severity falls with the age a loan charges off at.** Capped, it averages 0.93 in a loan's first
year, 0.74 at 48-59 months, 0.58 at 96-119 and 0.49 from 180 months on, and every five-year group
of vintages shows the same slope, so it is the loan's age and not the vintage mix. The fractional
logit carries the age at charge-off as a term, which doubles its out-of-time R-squared against a
constant (0.14 to 0.27), and the open book is priced month by month: the chance of charging
off at each future age times the severity for that age. Most open loans are young, and a young
loan that charges off loses more, so the term raises the 12-month EL by 12%.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
import statsmodels.api as sm
from scipy.stats import poisson

from credit_risk import capital, hazard, metrics, woe
from credit_risk.config import AS_OF, BORROWER_COLUMNS

COMPANY = ("CORPORATION", "PARTNERSHIP")
DROPPED_STATUSES = ("CANCLD", "COMMIT")
PAID, CHARGED_OFF, OPEN = "P I F", "CHGOFF", "EXEMPT"
DEVELOPMENT_VINTAGES = range(2000, 2010)  # resolved: 1.3% or less still EXEMPT in each
NEXT_VINTAGE = 2010
LEFT_TRUNCATION = pd.Timestamp("2005-08-01")  # the first month whose payoff dates are real
HOLDOUT_SHARE = 0.30
SEED = 20261101
BANDS = "ABCDEFGH"  # A is the highest score and the lowest risk
STATE_GROUPS = 6
MIN_AT_RISK = 500
SEVERITY_CAP = 1.0

FEATURE_SOURCES = (
    "GrossApproval", "SBAGuaranteedApproval", "NaicsCode", "FranchiseCode", "ProjectState",
    "ProcessingMethod", "BusinessType", "BusinessAge", "JobsSupported", "RevolverStatus",
)
LABEL_SOURCES = ("LoanStatus", "FirstDisbursementDate", "ChargeOffDate", "PaidInFullDate",
                 "GrossChargeOffAmount")
EXCLUDED_FEATURES = {
    "TermInMonths": "rewritten at charge-off",
    "CollateralInd": "attached at charge-off for FY2000-04 approvals",
    "InitialInterestRate": "absent before FY2005, complete only from FY2010",
    "FixedorVariableInterestInd": "absent wherever the rate is absent",
    "BankName": "current assignee, not the originator",
    "BankStreet": "current assignee's address",
    "BankCity": "current assignee's address",
    "BankState": "current assignee's address",
    "BankZip": "current assignee's address",
    "SoldSecMrktInd": "set after sale",
    "ChargeOffDate": "the outcome",
    "PaidInFullDate": "the outcome",
    "GrossChargeOffAmount": "the outcome",
    "LoanStatus": "the outcome",
}
READ_COLUMNS = ("AsOfDate", "LocationID", "ApprovalFY") + FEATURE_SOURCES + LABEL_SOURCES

FEATURES = ("amount_band", "guarantee_band", "sector", "state_group", "processing",
            "business_age", "jobs_band", "franchise", "facility", "business_type")

AMOUNT_EDGES = [0, 25e3, 50e3, 100e3, 150e3, 250e3, 500e3, 1e6, 2e6, np.inf]
AMOUNT_LABELS = ["<25k", "25-50k", "50-100k", "100-150k", "150-250k", "250-500k", "0.5-1m",
                 "1-2m", "2m+"]
GUARANTEE_EDGES = [-np.inf, 0.5, 0.74, 0.76, 0.84, 0.86, np.inf]
GUARANTEE_LABELS = ["<=50%", "51-74%", "75%", "76-84%", "85%", "86%+"]
JOBS_EDGES = [-np.inf, 0, 2, 5, 10, 25, np.inf]
JOBS_LABELS = ["0", "1-2", "3-5", "6-10", "11-25", "26+"]
ORDINAL_BINS = {"amount_band": AMOUNT_LABELS, "guarantee_band": GUARANTEE_LABELS,
                "jobs_band": JOBS_LABELS}

# SBA recoded BusinessAge in FY2018. Both codings are folded onto four groups that mean the same
# thing in each, so a FY2021 loan lands in a bin the FY2000-2009 scorecard has seen.
BUSINESS_AGE = {
    "Existing, 5 or more years": "existing 2y+",
    "Less than 5 years old but at least 4": "existing 2y+",
    "Less than 4 years old but at least 3": "existing 2y+",
    "Less than 3 years old but at least 2": "existing 2y+",
    "Existing or more than 2 years old": "existing 2y+",
    "Change of Ownership": "change of ownership",
    "New, Less than 1 Year old": "new under 2y",
    "New Business or 2 years or less": "new under 2y",
    "Startup, Loan Funds will Open Business": "startup",
    "Unanswered": "unanswered",
}


def file_tag(path):
    match = re.search(r"FOIA_7a_(.+?)_asof", Path(path).name)
    return match[1] if match else Path(path).stem


def read_loans(paths):
    """Read the FOIA CSVs without the borrower columns, minting ``loan_id`` per file and row.

    The file has no loan identifier: ``LocationID`` is the lender's location. ``loan_id`` is the
    file's fiscal-year span and the 1-based row number inside that file, so it is deterministic
    for a given extract and means nothing beyond it. A sample's ids are rows of the sample.
    """
    frames = []
    for path in sorted(paths, key=str):
        header = pd.read_csv(path, nrows=0).columns
        assert not set(READ_COLUMNS) & set(BORROWER_COLUMNS)
        missing = [c for c in READ_COLUMNS if c not in header]
        if missing:
            raise ValueError(f"{Path(path).name}: missing {missing}")
        frame = pd.read_csv(path, usecols=list(READ_COLUMNS),
                            dtype={"FranchiseCode": "str"}, low_memory=False)
        tag = file_tag(path)
        frame.insert(0, "loan_id", [f"{tag}:{i:07d}" for i in range(1, len(frame) + 1)])
        frames.append(frame)
    return pd.concat(frames, ignore_index=True)


def company_book(loans, as_of=AS_OF):
    """Company credit with its exit labels: corporations and partnerships, disbursed loans only."""
    book = loans[loans.BusinessType.isin(COMPANY) & ~loans.LoanStatus.isin(DROPPED_STATUSES)].copy()
    book["vintage"] = book.ApprovalFY.astype(int)
    book["disbursed"] = pd.to_datetime(book.FirstDisbursementDate, errors="coerce")
    as_of = pd.Timestamp(as_of)
    exit_date = pd.Series(as_of, index=book.index)
    exit_date = exit_date.where(book.LoanStatus != CHARGED_OFF,
                                pd.to_datetime(book.ChargeOffDate, errors="coerce"))
    exit_date = exit_date.where(book.LoanStatus != PAID,
                                pd.to_datetime(book.PaidInFullDate, errors="coerce"))
    book["exit_date"] = exit_date
    book["outcome"] = book.LoanStatus.map({CHARGED_OFF: "event", PAID: "competing",
                                           OPEN: "censored"})
    book["charged_off"] = (book.LoanStatus == CHARGED_OFF).astype(int)
    usable = book.disbursed.notna() & book.exit_date.notna() & (book.disbursed <= as_of)
    book["in_life_table"] = usable & (book.exit_date >= LEFT_TRUNCATION)
    book["entry_age"] = hazard.age_in_months(
        book.disbursed, pd.Series(LEFT_TRUNCATION, index=book.index)).where(book.in_life_table)
    book["exit_age"] = hazard.age_in_months(book.disbursed, book.exit_date).where(usable)
    book["age_now"] = hazard.age_in_months(book.disbursed, pd.Series(as_of, index=book.index))
    return book


# ------------------------------------------------------------------------------------------------
# Features
# ------------------------------------------------------------------------------------------------

@dataclass
class Binning:
    """The data-driven parts of the binning, learnt once on the development training sample."""
    state_map: dict = field(default_factory=dict)
    state_fallback: str = "g3"
    sectors: set = field(default_factory=set)
    processing: set = field(default_factory=set)
    kept: dict = field(default_factory=dict)  # nominal feature -> bins seen often enough to keep
    fallback: dict = field(default_factory=dict)  # nominal feature -> where an unseen bin goes
    ordinal_map: dict = field(default_factory=dict)  # ordered feature -> {bin: bin it scores as}

    def apply(self, book):
        out = pd.DataFrame(index=book.index)
        amount = book.GrossApproval.astype(float)
        out["amount_band"] = pd.cut(amount, AMOUNT_EDGES, labels=AMOUNT_LABELS).astype(str)
        share = book.SBAGuaranteedApproval.astype(float) / amount.where(amount > 0)
        out["guarantee_band"] = pd.cut(share, GUARANTEE_EDGES, labels=GUARANTEE_LABELS).astype(str)
        sector = sector_code(book.NaicsCode)
        out["sector"] = sector.where(sector.isin(self.sectors), "other")
        out["state_group"] = book.ProjectState.map(self.state_map).fillna(self.state_fallback)
        processing = book.ProcessingMethod.fillna("unknown")
        out["processing"] = processing.where(processing.isin(self.processing), "other")
        out["business_age"] = book.BusinessAge.map(BUSINESS_AGE).fillna("unanswered")
        out["jobs_band"] = pd.cut(book.JobsSupported.astype(float), JOBS_EDGES,
                                  labels=JOBS_LABELS).astype(str).replace("nan", "0")
        code = book.FranchiseCode.fillna("").str.strip()
        out["franchise"] = np.where(code.isin(["", "0", "00000"]), "independent", "franchise")
        out["facility"] = book.RevolverStatus.map({"Y": "revolver", "N": "term"}).fillna("term")
        out["business_type"] = book.BusinessType.str.lower()
        for feature, kept in self.kept.items():
            out[feature] = out[feature].where(out[feature].isin(kept), self.fallback[feature])
        for feature, mapping in self.ordinal_map.items():
            out[feature] = out[feature].map(mapping).fillna(mapping["nan"])
        return out


def sector_code(naics):
    two = naics.dropna().astype(np.int64).astype(str).str[:2].reindex(naics.index).fillna("unknown")
    return two.replace({"32": "31-33", "33": "31-33", "31": "31-33", "45": "44-45",
                        "44": "44-45", "49": "48-49", "48": "48-49"})


def nearest_kept(values, labels, min_count):
    """Map every declared label, and "nan", onto the nearest label with at least min_count rows."""
    counts = values.value_counts()
    kept = [i for i, label in enumerate(labels) if counts.get(label, 0) >= min_count]
    mapping = {label: labels[min(kept, key=lambda k: (abs(k - i), k))]
               for i, label in enumerate(labels)}
    mapping["nan"] = counts.reindex(labels).idxmax()
    return mapping


def fit_binning(train, y, min_count):
    binning = Binning()
    sector = sector_code(train.NaicsCode)
    _, binning.sectors = woe.fold_rare(sector, min_count)
    _, binning.processing = woe.fold_rare(train.ProcessingMethod.fillna("unknown"), min_count)
    binning.state_map = woe.coarse_class(train.ProjectState.fillna("unknown"), y, STATE_GROUPS)
    binning.state_fallback = f"g{(STATE_GROUPS + 1) // 2}"
    # Second pass on the finished bins. An ordered characteristic folds a rare bin into its
    # nearest kept neighbour -- the course's coarse classing -- so a loan above $2m, which the
    # FY2000-09 programme barely made (the 7(a) cap was $2m until the Small Business Jobs Act of
    # 2010), scores as the largest amount the card has seen, not as an "other" lumped with
    # whatever else was rare. A nominal one folds rare bins into "other"; a bin never seen in
    # training goes to "other" if training had one, else to the commonest bin, and an "other"
    # that is itself under min_count joins the commonest bin rather than standing alone.
    binned = binning.apply(train)
    for feature, labels in ORDINAL_BINS.items():
        binning.ordinal_map[feature] = nearest_kept(binned[feature], labels, min_count)
    for feature, values in binned.items():
        if feature in ORDINAL_BINS:
            continue
        folded, _ = woe.fold_rare(values, min_count)
        counts = folded.value_counts()
        common = counts.index[0]
        if counts.get("other", min_count) < min_count:
            folded = folded.replace("other", common)
        binning.kept[feature] = set(folded.unique())
        binning.fallback[feature] = "other" if "other" in binning.kept[feature] else common
    return binning


# ------------------------------------------------------------------------------------------------
# PD scorecard
# ------------------------------------------------------------------------------------------------

def stratified_holdout(keys, share, seed):
    """A boolean mask selecting ``share`` of each key's rows, deterministic in ``seed``."""
    rng = np.random.default_rng(seed)
    keys = pd.Series(keys).reset_index(drop=True)
    mask = np.zeros(len(keys), dtype=bool)
    for _, idx in keys.groupby(keys).indices.items():
        idx = np.asarray(idx)
        chosen = rng.permutation(idx)[: int(round(share * len(idx)))]
        mask[chosen] = True
    return mask


@dataclass
class PDModel:
    binning: Binning
    card: woe.Scorecard
    band_edges: np.ndarray
    train_ids: set
    holdout: pd.DataFrame
    holdout_overall: dict
    holdout_by_vintage: pd.DataFrame
    next_vintage: dict
    iv: pd.DataFrame
    cutoffs: pd.DataFrame
    train_scores: np.ndarray

    def features(self, book):
        return self.binning.apply(book)

    def score(self, book):
        return self.card.score(self.features(book))

    def band(self, scores):
        index = np.searchsorted(self.band_edges, scores, side="right")
        return np.array(list(BANDS))[::-1][index]


def fit_pd(book, n_boot=500):
    """Scorecard on the resolved FY2000-2009 vintages; held out within vintage; FY2010 after."""
    resolved = book[book.LoanStatus.isin([PAID, CHARGED_OFF])]
    dev = resolved[resolved.vintage.isin(DEVELOPMENT_VINTAGES)].reset_index(drop=True)
    in_holdout = stratified_holdout(dev.vintage.astype(str) + "|" + dev.charged_off.astype(str),
                                    HOLDOUT_SHARE, SEED)
    train, test = dev[~in_holdout], dev[in_holdout]
    min_count = max(30, int(0.002 * len(train)))
    binning = fit_binning(train, train.charged_off, min_count)
    X_train = binning.apply(train)
    X_train["vintage"] = train.vintage.astype(str).values
    card = woe.fit_scorecard(X_train, train.charged_off.values, FEATURES, level="vintage",
                             groups=train.LocationID.fillna(-1).values)
    train_scores = card.score(X_train)
    edges = np.quantile(train_scores, np.linspace(0, 1, len(BANDS) + 1)[1:-1])

    model = PDModel(binning=binning, card=card, band_edges=edges, train_ids=set(train.loan_id),
                    holdout=None, holdout_overall=None, holdout_by_vintage=None,
                    next_vintage=None, iv=None, cutoffs=None, train_scores=train_scores)
    test = test.assign(score=model.score(test))
    model.holdout = test[["loan_id", "vintage", "charged_off", "score"]]
    model.holdout_overall = metrics.discrimination(test.charged_off, -test.score, n_boot)
    rows = []
    for vintage, part in test.groupby("vintage"):
        rows.append({"vintage": vintage,
                     **metrics.discrimination(part.charged_off, -part.score, n_boot)})
    model.holdout_by_vintage = pd.DataFrame(rows)
    nxt = resolved[resolved.vintage == NEXT_VINTAGE]
    model.next_vintage = metrics.discrimination(nxt.charged_off, -model.score(nxt), n_boot)
    model.iv = pd.DataFrame(
        [{"feature": f, "iv": woe.woe_table(X_train[f], train.charged_off).iv.sum()}
         for f in FEATURES]).sort_values("iv", ascending=False)
    cut_points = np.quantile(test.score, [0.05, 0.10, 0.20, 0.30, 0.40, 0.50])
    model.cutoffs = woe.cutoff_table(test.score, test.charged_off, np.round(cut_points))
    return model


def psi_by_vintage(model, book):
    """Score PSI of every vintage's loans against the development training scores."""
    edges = np.concatenate([[-np.inf], model.band_edges, [np.inf]])
    scores = model.score(book)
    rows = []
    for vintage, idx in book.groupby("vintage").indices.items():
        rows.append({"vintage": vintage, "loans": len(idx),
                     "psi": metrics.psi(model.train_scores, scores[idx], edges)})
    return pd.DataFrame(rows)


# ------------------------------------------------------------------------------------------------
# Hazard, calibrated per band
# ------------------------------------------------------------------------------------------------

CREDIBILITY_EVENTS = 100  # expected charge-offs at which a vintage's experience gets half weight


@dataclass
class HazardModel:
    curve: hazard.HazardCurve
    bands: pd.DataFrame  # one row per band: multipliers, PD at origination
    book_lifetime_pd: float
    experience: pd.DataFrame = None  # one row per vintage: observed against expected charge-offs
    stage2: list = field(default_factory=list)  # vintages in stage 2

    def pd(self, age, band, horizon, factor=None):
        """Charge-off PD over ``horizon`` months from ``age``, per loan, for its band.

        ``horizon`` is one number of months or one per loan; None runs to the end of the curve.
        ``factor`` multiplies the band's event hazard loan by loan; the point-in-time
        sensitivity passes each loan's vintage experience factor, everything else passes None.
        """
        age = np.asarray(age, dtype=int)
        band = np.asarray(band)
        if horizon is not None:
            horizon = np.broadcast_to(np.asarray(horizon, dtype=int), age.shape)
        factor = np.ones(len(age)) if factor is None else np.asarray(factor, dtype=float)
        out = np.zeros(len(age))
        for label, row in self.bands.iterrows():
            for value in np.unique(factor[band == label]):
                mask = (band == label) & (factor == value)
                out[mask] = self.curve.conditional_incidence(
                    age[mask], None if horizon is None else horizon[mask],
                    row.event_multiplier * value, row.competing_multiplier)
        return out

    def pd_by_age(self, age, band, horizon, starts, factor=None):
        """``pd`` split by the loan age the charge-off would come at: one column per age band.

        ``starts`` are each band's first month and the last band is open-ended, so every row
        sums to ``pd`` over the same horizon.
        """
        age = np.asarray(age, dtype=int)
        stop = np.maximum(age, self.curve.max_age) if horizon is None else age + horizon
        reached = [self.pd(age, band, np.clip(start, age, stop) - age, factor)
                   for start in starts[1:]]
        reached = [np.zeros(len(age)), *reached, self.pd(age, band, stop - age, factor)]
        return np.diff(np.column_stack(reached), axis=1)


def fit_hazard(model, book, min_at_risk=MIN_AT_RISK):
    """Pooled life table, band multipliers, and each vintage's charge-offs against the curve."""
    table = book[book.in_life_table]
    exit_age = table.exit_age.astype(int).values
    entry_age = table.entry_age.astype(int).values
    curve = hazard.estimate(exit_age, table.outcome, min_at_risk, entry_age)
    band = model.band(model.score(table))
    rows = hazard.band_multipliers(curve, exit_age, table.outcome.values, band, entry_age)
    bands = pd.DataFrame(rows).T.reindex(list(BANDS)).dropna(how="all").astype(float)
    zero = np.zeros(1, dtype=int)
    bands["pd_12m_at_origination"] = [
        curve.conditional_incidence(zero, 12, r.event_multiplier, r.competing_multiplier)[0]
        for r in bands.itertuples()]
    bands["lifetime_pd_at_origination"] = [
        curve.conditional_incidence(zero, None, r.event_multiplier, r.competing_multiplier)[0]
        for r in bands.itertuples()]
    fitted = HazardModel(curve, bands, curve.conditional_incidence(zero, None)[0])

    expected_event, _ = hazard.cumulative_hazard(curve, exit_age, entry_age)
    multiplier = pd.Series(band).map(bands.event_multiplier).values
    frame = pd.DataFrame({"vintage": table.vintage.values,
                          "observed": table.charged_off.values,
                          "expected": expected_event * multiplier,
                          "open": (table.outcome == "censored").values})
    frame = frame[exit_age < curve.max_age]
    experience = frame.groupby("vintage").agg(loans=("observed", "size"), open=("open", "sum"),
                                              observed=("observed", "sum"),
                                              expected=("expected", "sum"))
    experience["observed_over_expected"] = experience.observed / experience.expected
    experience["p_value"] = poisson.sf(experience.observed - 1, experience.expected)
    experience["credibility_factor"] = ((experience.observed + CREDIBILITY_EVENTS)
                                        / (experience.expected + CREDIBILITY_EVENTS))
    fitted.experience = experience
    fitted.stage2 = capital.stage2_vintages(experience)
    return fitted


# ------------------------------------------------------------------------------------------------
# Gross charge-off severity
# ------------------------------------------------------------------------------------------------

SEVERITY_TRAIN_VINTAGES = range(2000, 2010)
SEVERITY_TEST_VINTAGES = range(2010, 2020)
SEVERITY_AGE_STARTS = (0, 12, 24, 36, 48, 60, 72, 96, 120, 180)  # months to charge-off
SEVERITY_AGE_LABELS = [f"{a}-{b - 1}m" for a, b in zip(SEVERITY_AGE_STARTS,
                                                         SEVERITY_AGE_STARTS[1:])] + ["180m+"]
SEVERITY_TERMS = (*FEATURES, "age_band")


def age_band(months):
    edges = [*SEVERITY_AGE_STARTS, np.inf]
    return pd.cut(months, edges, right=False, labels=SEVERITY_AGE_LABELS).astype(str)


def severity_frame(book, rule="cap"):
    """Company charge-offs with a positive amount and their severity under one rule for > 1."""
    charged = book[(book.LoanStatus == CHARGED_OFF) & (book.GrossChargeOffAmount > 0)
                   & (book.GrossApproval > 0)].copy()
    charged["severity_raw"] = charged.GrossChargeOffAmount / charged.GrossApproval
    if rule == "cap":
        charged["severity"] = charged.severity_raw.clip(upper=SEVERITY_CAP)
    elif rule == "drop":
        charged = charged[charged.severity_raw <= SEVERITY_CAP].copy()
        charged["severity"] = charged.severity_raw
    elif rule == "uncapped":
        charged["severity"] = charged.severity_raw
    else:
        raise ValueError(rule)
    return charged


@dataclass
class SeverityModel:
    reference: dict
    result: object
    constant: float
    out_of_time: dict
    deciles: pd.DataFrame

    def design(self, X):
        parts = {"const": np.ones(len(X))}
        for feature, levels in self.reference.items():
            values = X[feature].astype(str).values
            for level in levels:
                parts[f"{feature}={level}"] = (values == level).astype(float)
        return pd.DataFrame(parts, index=X.index)

    def predict(self, X):
        return np.asarray(self.result.predict(self.design(X)))

    def by_age(self, X):
        """Each loan's severity were it charged off in each age band: (loans, age bands)."""
        return np.column_stack([self.predict(X.assign(age_band=label))
                                for label in SEVERITY_AGE_LABELS])


def _fit_fractional_logit(X, y):
    reference = {}
    for feature in SEVERITY_TERMS:
        counts = X[feature].astype(str).value_counts()
        reference[feature] = [level for level in counts.index[1:]]  # most common is the base
    model = SeverityModel(reference, None, float(np.mean(y)), {}, None)
    design = model.design(X)
    # Papke-Wooldridge fractional logit: a Bernoulli likelihood on a [0, 1] outcome, with
    # sandwich standard errors because the variance is not the binomial one.
    model.result = sm.GLM(np.asarray(y, float), design,
                          family=sm.families.Binomial()).fit(cov_type="HC1")
    return model


def fit_severity(pd_model, book, rule="cap"):
    """Fractional logit on origination fields and the age at charge-off, trained FY2000-09,
    scored on FY2010-19 at each charge-off's own age."""
    charged = severity_frame(book, rule)
    charged = charged[charged.exit_age.notna()]  # no disbursement or charge-off date, no age
    X = pd_model.features(charged)
    for feature in FEATURES:  # unseen bins go to "other" exactly as the scorecard folds them
        X[feature] = X[feature].astype(str)
    X["age_band"] = age_band(charged.exit_age)
    train = charged.vintage.isin(SEVERITY_TRAIN_VINTAGES).values
    test = charged.vintage.isin(SEVERITY_TEST_VINTAGES).values
    model = _fit_fractional_logit(X[train], charged.severity[train])
    predicted = model.predict(X[test])
    actual = charged.severity[test].values
    constant = np.full(len(actual), model.constant)
    sst = ((actual - constant) ** 2).sum()
    model.out_of_time = {
        "train_charge_offs": int(train.sum()), "test_charge_offs": int(test.sum()),
        "constant": model.constant, "test_mean": float(actual.mean()),
        "mae_model": float(np.abs(actual - predicted).mean()),
        "mae_constant": float(np.abs(actual - constant).mean()),
        "rmse_model": float(np.sqrt(((actual - predicted) ** 2).mean())),
        "rmse_constant": float(np.sqrt(((actual - constant) ** 2).mean())),
        "r2_vs_constant": float(1 - ((actual - predicted) ** 2).sum() / sst),
        "rank_corr": float(pd.Series(predicted).corr(pd.Series(actual), method="spearman")),
    }
    deciles = pd.qcut(pd.Series(predicted).rank(method="first"), 10, labels=False)
    model.deciles = pd.DataFrame({"decile": deciles + 1, "predicted": predicted,
                                  "actual": actual}).groupby("decile").mean().reset_index()
    return model


# ------------------------------------------------------------------------------------------------
# The open book on the as-of date
# ------------------------------------------------------------------------------------------------

def live_book(pd_model, hazard_model, severity_model, book):
    """Every EXEMPT company loan with a disbursement date: PD, severity, EL, stage and capital.

    Severity depends on the age a loan charges off at, so each horizon's EL is a sum over age
    bands: the chance of charging off inside the band before the horizon ends, times the
    severity predicted for that band. ``severity`` is the 12-month figure that sum implies, so
    ``el_12m = pd_12m * severity * exposure`` still holds and capital takes it as its LGD; a loan
    whose 12-month PD is zero carries its severity at today's age instead.
    """
    open_ = book[(book.LoanStatus == OPEN) & book.in_life_table].copy()
    X = pd_model.features(open_)
    open_["score"] = pd_model.card.score(X)
    open_["band"] = pd_model.band(open_.score.values)
    age = open_.age_now.astype(int).values
    severity = severity_model.by_age(X)

    def priced(horizon, factor=None):
        """``(pd, pd x severity)`` per loan over the horizon."""
        split = hazard_model.pd_by_age(age, open_.band.values, horizon, SEVERITY_AGE_STARTS,
                                       factor)
        return split.sum(axis=1), (split * severity).sum(axis=1)

    open_["pd_12m"], loss_12m = priced(12)
    open_["pd_lifetime"], loss_lifetime = priced(None)
    today = severity[np.arange(len(age)), np.searchsorted(SEVERITY_AGE_STARTS, age, "right") - 1]
    open_["severity"] = np.divide(loss_12m, open_.pd_12m.values, out=today,
                                  where=open_.pd_12m.values > 0)
    open_["exposure"] = open_.GrossApproval.astype(float)
    open_["el_12m"] = loss_12m * open_.exposure
    open_["el_lifetime"] = loss_lifetime * open_.exposure
    open_["stage"] = np.where(open_.vintage.isin(hazard_model.stage2), 2, 1)
    open_["ecl"] = np.where(open_.stage == 2, open_.el_lifetime, open_.el_12m)
    open_["capital_k"] = capital.irb_capital(open_.pd_12m, open_.severity)
    open_["rwa"] = 12.5 * open_.capital_k * open_.exposure
    open_["guaranteed_share"] = (open_.SBAGuaranteedApproval / open_.exposure).clip(0, 1)
    # Point-in-time sensitivity, not the booked ECL: each vintage's charge-offs so far against
    # the through-the-cycle curve, shrunk towards 1 by credibility, scale its remaining hazard.
    factor = open_.vintage.map(hazard_model.experience.credibility_factor).fillna(1.0).values
    open_["pit_factor"] = factor
    _, pit_12m = priced(12, factor)
    _, pit_life = priced(None, factor)
    open_["ecl_pit"] = open_.exposure * np.where(open_.stage == 2, pit_life, pit_12m)
    columns = ["loan_id", "vintage", "age_now", "score", "band", "pd_12m", "pd_lifetime",
               "severity", "exposure", "el_12m", "el_lifetime", "stage", "ecl", "pit_factor",
               "ecl_pit", "capital_k", "rwa", "guaranteed_share"]
    return open_[columns].reset_index(drop=True)


def book_summary(live):
    total = live.exposure.sum()
    return {
        "loans": int(len(live)),
        "exposure": float(total),
        "el_12m": float(live.el_12m.sum()),
        "el_lifetime": float(live.el_lifetime.sum()),
        "ecl_staged": float(live.ecl.sum()),
        "ecl_point_in_time": float(live.ecl_pit.sum()),
        "stage2_loans": int((live.stage == 2).sum()),
        "stage2_exposure": float(live.exposure[live.stage == 2].sum()),
        "rwa": float(live.rwa.sum()),
        "capital": float((live.capital_k * live.exposure).sum()),
        "pd_12m_weighted": float((live.pd_12m * live.exposure).sum() / total),
        "severity_weighted": float((live.severity * live.exposure).sum() / total),
        "guaranteed_exposure": float((live.guaranteed_share * live.exposure).sum()),
    }


def severity_sensitivity(pd_model, hazard_model, severity_model, book):
    """The open book's EL under each rule for charge-offs above their original amount.

    The fitted model is kept and only its level moves: each rule's mean severity on the training
    charge-offs, over the capped mean the model was fitted to. A fractional logit cannot be refitted
    to the uncapped values -- they run to several hundred times the loan, and its Bernoulli
    variance has no room above 1 -- and refitting on the dropped rows would change two things at
    once. EL is linear in severity, so this isolates exactly what the rule changes.
    """
    live = live_book(pd_model, hazard_model, severity_model, book)
    capped = severity_frame(book, "cap")
    base = capped.severity[capped.vintage.isin(SEVERITY_TRAIN_VINTAGES)].mean()
    rows = []
    for rule in ("cap", "drop", "uncapped"):
        charged = severity_frame(book, rule)
        train_mean = charged.severity[charged.vintage.isin(SEVERITY_TRAIN_VINTAGES)].mean()
        factor = train_mean / base
        rows.append({"rule": rule, "charge_offs": len(charged),
                     "mean_severity": float(charged.severity.mean()),
                     "median_severity": float(charged.severity.median()),
                     "level_factor": float(factor),
                     "book_el_12m": float(live.el_12m.sum() * factor),
                     "book_el_lifetime": float(live.el_lifetime.sum() * factor)})
    return pd.DataFrame(rows)


def recovery_context(recovery):
    """Table 11 and Table 10 for 7(a) Regular, side by side and never summed."""
    regular = recovery[recovery.program == "7(a) Regular"]
    totals = regular.groupby(["table", "cohort_year"]).cohort_total_pct.first().unstack(0)
    return totals.rename(columns={"post_charge_off": "post_charge_off_pct_of_charged_off",
                                  "total_recovery": "total_recovery_pct_of_purchase"})
