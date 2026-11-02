"""The FDIC chain: which banks fail, what a failure costs the fund, and how deposits run before it.

    failure_events()     FAILURE rows, one per CERT; ASSISTANCE kept apart as a censoring date
    pd_panel()           every bank at every year-end, labelled with failure in the next 12 months
    fit_pd()             logistic PD on filing ratios, p-values clustered on the bank
    loss_sample()        failures with COST, deposits at failure, and the filing ~12 months before
    fit_loss()           insurer loss rate, one stage, judged first out of time
    runoff_sample()      deposits at failure over deposits a year earlier, as a table
    el_backtest()        PD x loss rate x deposits against the COST the fund actually booked

Every dollar figure from the FDIC API -- ``ASSET``, ``DEP``, ``QBFDEP``, ``COST`` -- is in
thousands, and keeps that unit here; a column holding one ends in ``_k``.

This is bank and counterparty risk, presented beside the SBA book and never multiplied into it.
An SBA loan's lender failing and an SBA borrower charging off are different events on different
populations, and a product of the two chains would describe neither.

THE LABEL, STATED ONCE AND APPLIED ONCE
---------------------------------------

A row is a bank's year-end filing. Its horizon starts one quarter after the filing date -- the
embargo, because a December call report is not filed until well into the next quarter -- and
runs twelve months:

    failure inside (REPDTE + 3m, REPDTE + 15m]   y = 1
    failure inside (REPDTE, REPDTE + 3m]         dropped: the embargo, and the row before it
                                                 already holds this failure in its horizon
    failure on or before REPDTE                  dropped: filed by a bank already closed
    assistance on or before the horizon's end    dropped, and every row after it: open-bank
                                                 assistance is not failure, and what the bank
                                                 would have done without it is unobserved
    no filing at the next year-end, no failure   dropped: a censored exit. Mergers are not in the
                                                 failures table, so a bank that stops filing
                                                 without failing has left for a reason this
                                                 table cannot see
    otherwise                                    y = 0
    horizon ending after the data ends           dropped: not yet observed, even when the bank
                                                 has already failed inside it, because the banks
                                                 that will not fail cannot be labelled yet

``exits="keep"`` changes only the second-last-but-one line: censored exits stay in as
non-failures. That is the variant most published bank-failure models use without saying so, and
both are reported.

Only FAILURE is a default, dated by its own FAILDATE. Citibank N.A. and Bank of America N.A.
both appear as ASSISTANCE events and both are still open. 37 CERTs were assisted and failed
later; their rows stop at the assistance, so their later failure is not counted. CERT is the
dedupe key: 42 rows are commented as FSLIC-RTC duplicates, 21 pairs, and 3 of those pairs share
a CERT. The earlier event of each pair is kept.

WHY THE EQUITY RATIO ALONE SCORES SO HIGH
-----------------------------------------

Failure is not only an economic event. Since FDICIA in 1991, prompt corrective action requires a
critically undercapitalised bank -- tangible equity at or below 2% of assets -- to be put into
receivership within 90 days unless its regulator acts otherwise. A bank's capital ratio is
therefore close to part of the definition of the event the model predicts, and an equity ratio
that ranks failures at an AUC above 0.9 is partly measuring the closure rule. It is reported as
the benchmark the full model has to beat, not as a discovery.

FEATURES COME ONLY FROM FILINGS
-------------------------------

Equity to assets, ROA, net interest margin, noncurrent loans, net charge-offs, loans to deposits
and the uninsured share of deposits. Never the resolution type, the acquirer or anything from
``institutions.csv``, whose fields describe the bank as it is today. ``RBCRWAJ`` is left out: it
is 0 or above 100 for every bank in 1984-87, before risk-based capital existed, and 0 for 35-39%
of banks at every year-end from 2020, when community banks could opt into the leverage-ratio
framework instead. Total
assets were tested and dropped (p = 0.91). ``DEPINS`` exceeds total deposits on 4-6% of filings
before 1991, so the uninsured share is clipped to [0, 1].
"""

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import statsmodels.api as sm

from credit_risk import metrics

FEATURES = ("EQV", "ROA", "NIMY", "NCLNLSR", "NTLNLSR", "LNLSDEPR", "unins_share")
WINDOWS = {"train": (1985, 2007), "validate": (2008, 2012), "test": (2013, 2025)}
EMBARGO_MONTHS, HORIZON_MONTHS = 3, 12
CALIBRATION_EDGES = [0, 0.001, 0.005, 0.02, 0.10, 1.0001]
SYSTEMIC_2023 = {24735: "Silicon Valley Bank", 57053: "Signature Bank",
                 59017: "First Republic Bank"}
FSLIC_DUPLICATE_COMMENT = "FSLIC-RTC Duplicate Failures"
RUNOFF_WINDOW_MONTHS = (9, 15)
LOSS_TRAIN = (1986, 2007)  # FDIC: loss data before 1986 are not comprehensive
ERAS = [(1984, 1991, "1984-91"), (1992, 2007, "1992-2007"), (2008, 2012, "2008-12"),
        (2013, 2030, "2013-26")]


def window_of(year):
    for name, (low, high) in WINDOWS.items():
        if low <= year <= high:
            return name
    return None


def era_of(year):
    for low, high, label in ERAS:
        if low <= year <= high:
            return label
    return "pre-1984"


def month_end(dates, months):
    return (dates.dt.to_period("M") + months).dt.to_timestamp(how="end").dt.normalize()


# ------------------------------------------------------------------------------------------------
# Events
# ------------------------------------------------------------------------------------------------

def failure_events(failures):
    """``(events, assisted, counts)``: one FAILURE per CERT, first ASSISTANCE date per CERT."""
    frame = failures.copy()
    frame["fail_date"] = pd.to_datetime(frame.FAILDATE, format="%m/%d/%Y")
    failed = frame[(frame.RESTYPE == "FAILURE") & frame.CERT.notna()].sort_values("fail_date")
    events = failed.drop_duplicates("CERT", keep="first").copy()
    events["CERT"] = events.CERT.astype(np.int64)
    assisted = (frame[(frame.RESTYPE == "ASSISTANCE") & frame.CERT.notna()]
                .sort_values("fail_date").drop_duplicates("CERT")
                .rename(columns={"fail_date": "assist_date"})[["CERT", "assist_date"]])
    assisted["CERT"] = assisted.CERT.astype(np.int64)
    counts = {
        "rows": len(frame),
        "failure_rows": int((frame.RESTYPE == "FAILURE").sum()),
        "assistance_rows": int((frame.RESTYPE == "ASSISTANCE").sum()),
        "failure_rows_without_cert": int(((frame.RESTYPE == "FAILURE") & frame.CERT.isna()).sum()),
        "duplicate_failures_removed": len(failed) - len(events),
        "fslic_rtc_commented_rows": int((frame.COMMENTS == FSLIC_DUPLICATE_COMMENT).sum()),
        "assisted_then_failed": int(events.CERT.isin(assisted.CERT).sum()),
    }
    return events, assisted, counts


# ------------------------------------------------------------------------------------------------
# PD
# ------------------------------------------------------------------------------------------------

def add_features(frame):
    out = frame.copy()
    deposits = out.DEP.where(out.DEP > 0)
    out["unins_share"] = (out.DEPUNINS / deposits).clip(0, 1)
    return out


def pd_panel(yearend, failures, exits="censor"):
    """Year-end filings labelled with failure in the 12 months after a one-quarter embargo."""
    if exits not in ("censor", "keep"):
        raise ValueError(exits)
    events, assisted, _ = failure_events(failures)
    panel = yearend.copy()
    panel["repdte"] = pd.to_datetime(panel.REPDTE.astype(str), format="%Y%m%d")
    panel["year"] = panel.repdte.dt.year
    following = panel[["CERT", "year"]].assign(year=lambda d: d.year - 1, files_next=True)
    panel = (panel.merge(following, on=["CERT", "year"], how="left")
             .merge(events[["CERT", "fail_date"]], on="CERT", how="left")
             .merge(assisted, on="CERT", how="left"))
    panel["files_next"] = panel.files_next.fillna(False).astype(bool)
    start = month_end(panel.repdte, EMBARGO_MONTHS)
    end = month_end(panel.repdte, EMBARGO_MONTHS + HORIZON_MONTHS)
    observed_through = max(events.fail_date.max(), panel.repdte.max())

    hit = (panel.fail_date > start) & (panel.fail_date <= end)
    # Later lines win, so the order below is the precedence, weakest first.
    reasons = pd.Series("kept", index=panel.index)
    if exits == "censor":
        reasons[~hit & ~panel.files_next] = "censored exit"
    reasons[end > observed_through] = "horizon not yet observed"
    reasons[panel.assist_date.notna() & (panel.assist_date <= end)] = "assisted"
    reasons[panel.fail_date.notna() & (panel.fail_date <= panel.repdte)] = "after failure"
    reasons[(panel.fail_date > panel.repdte) & (panel.fail_date <= start)] = "embargo"
    panel["label"] = hit.astype(int)
    panel["horizon_year"] = panel.year + 1
    panel["window"] = panel.horizon_year.map(window_of)
    panel["drop_reason"] = reasons
    return add_features(panel)


@dataclass
class PDModel:
    features: tuple
    limits: dict
    medians: dict
    result: object = None

    def design(self, frame):
        X = pd.DataFrame({c: frame[c].clip(*self.limits[c]).fillna(self.medians[c])
                          for c in self.features}, index=frame.index)
        return sm.add_constant(X, has_constant="add")

    def predict(self, frame):
        return np.asarray(self.result.predict(self.design(frame)))

    def coefficients(self):
        table = pd.DataFrame({"coef": self.result.params, "std_err": self.result.bse,
                              "p_value": self.result.pvalues})
        table.index.name = "term"
        return table.reset_index()


def fit_pd(rows, features=FEATURES):
    """Logistic PD, winsorised at the fitting rows' 1st/99th percentiles, clustered on CERT."""
    limits = {c: tuple(rows[c].quantile([0.01, 0.99])) for c in features}
    medians = {c: float(rows[c].median()) for c in features}
    model = PDModel(tuple(features), limits, medians)
    groups = pd.factorize(rows.CERT)[0]
    model.result = sm.Logit(rows.label.astype(float), model.design(rows)).fit(
        disp=0, maxiter=200, cov_type="cluster", cov_kwds={"groups": groups})
    return model


def evaluate_pd(panel, n_boot=500):
    """Train on 1985-2007 horizons; score validate and test; the equity ratio as benchmark."""
    rows = panel[panel.drop_reason == "kept"]
    train = rows[rows.window == "train"]
    full, equity = fit_pd(train), fit_pd(train, ("EQV",))
    table, calibration, psi = [], {}, {}
    edges = np.quantile(full.predict(train), np.linspace(0, 1, 11))
    edges[0], edges[-1] = -np.inf, np.inf
    for name in WINDOWS:
        part = rows[rows.window == name]
        p_full, p_equity = full.predict(part), equity.predict(part)
        table.append({"window": name, "model": "full",
                      **metrics.discrimination(part.label, p_full, n_boot)})
        table.append({"window": name, "model": "equity ratio only",
                      **metrics.discrimination(part.label, p_equity, n_boot)})
        calibration[name] = metrics.calibration_table(part.label, p_full, CALIBRATION_EDGES)
        psi[name] = metrics.psi(full.predict(train), p_full, edges)
    return {"model": full, "equity_model": equity, "discrimination": pd.DataFrame(table),
            "calibration": calibration, "score_psi": psi}


# ------------------------------------------------------------------------------------------------
# Loss given failure: the insurer loss rate
# ------------------------------------------------------------------------------------------------

def filing_before(events, prefailure, months=RUNOFF_WINDOW_MONTHS):
    """Each failed bank's filing nearest 12 months before failure, inside the 9-15 month window."""
    filings = prefailure.copy()
    filings["repdte"] = pd.to_datetime(filings.REPDTE.astype(str), format="%Y%m%d")
    joined = filings.merge(events[["CERT", "fail_date"]], on="CERT")
    joined["lag_months"] = (joined.fail_date - joined.repdte).dt.days / 30.4375
    joined = joined[joined.lag_months.between(*months)]
    joined["distance"] = (joined.lag_months - 12).abs()
    nearest = joined.sort_values(["CERT", "distance", "repdte"]).drop_duplicates("CERT")
    return add_features(nearest.drop(columns=["fail_date", "distance"]))


def loss_sample(failures, prefailure):
    """Failures with COST and deposits, the ~12-month filing, and every flag the brief names."""
    events, _, _ = failure_events(failures)
    events = events[(events.FAILYR >= 1984) & events.COST.notna() & (events.QBFDEP > 0)]
    sample = events.merge(filing_before(events, prefailure), on="CERT", how="inner")
    sample["loss_rate"] = sample.COST / sample.QBFDEP
    sample["loss_rate_assets"] = sample.COST / sample.QBFASSET
    sample["era"] = sample.FAILYR.map(era_of)
    sample["pre_1992"] = (sample.FAILYR <= 1991).astype(float)
    sample["window"] = sample.FAILYR.map(window_of)
    sample["flag_1984_85"] = sample.FAILYR <= 1985
    sample["flag_fslic"] = sample.SAVR == "FSLIC"
    sample["flag_cost_not_positive"] = sample.COST <= 0
    sample["flag_systemic_2023"] = sample.CERT.isin(SYSTEMIC_2023)
    return sample


@dataclass
class LossModel:
    features: tuple
    limits: dict
    medians: dict
    result: object = None
    terms: list = field(default_factory=list)

    def design(self, frame):
        X = pd.DataFrame({c: frame[c].clip(*self.limits[c]).fillna(self.medians[c])
                          for c in self.features}, index=frame.index)
        X["pre_1992"] = frame.pre_1992.values
        return sm.add_constant(X, has_constant="add")[self.terms]

    def predict(self, frame):
        return np.asarray(self.result.predict(self.design(frame)))


def fit_loss(rows, features=FEATURES):
    """One-stage OLS on the loss rate, with the pre-1992 regime as its era term."""
    limits = {c: tuple(rows[c].quantile([0.01, 0.99])) for c in features}
    medians = {c: float(rows[c].median()) for c in features}
    model = LossModel(tuple(features), limits, medians)
    X = sm.add_constant(pd.DataFrame({c: rows[c] for c in features}), has_constant="add")
    X["pre_1992"] = rows.pre_1992.values
    model.terms = ["const", *features, "pre_1992"] if rows.pre_1992.nunique() > 1 else [
        "const", *features]
    model.result = sm.OLS(rows.loss_rate.values, model.design(rows)).fit(cov_type="HC1")
    return model


def loss_training_rows(sample):
    years = sample.FAILYR.between(*LOSS_TRAIN)
    return sample[years & ~sample.flag_fslic & ~sample.flag_1984_85]


def loss_out_of_time(sample):
    """Fit on 1986-2007, score 2008-12 and 2013-26: era-only against era plus financials.

    The era-only model is the 1992-2007 mean, because every test failure is post-FDICIA, and
    that is also the constant the financials have to beat. 2008 onward has no era term the
    training years could estimate, which is the whole point of testing out of time.
    """
    train = loss_training_rows(sample)
    model = fit_loss(train)
    era_only = float(train.loss_rate[train.pre_1992 == 0].mean())
    rows = []
    for name in ("validate", "test"):
        for systemic in (True, False):
            part = sample[(sample.window == name) | ((name == "test") & (sample.FAILYR > 2025))]
            if not systemic:
                part = part[~part.flag_systemic_2023]
            actual, predicted = part.loss_rate.values, model.predict(part)
            constant = np.full(len(actual), era_only)
            rows.append({
                "window": name, "systemic_2023": "included" if systemic else "excluded",
                "failures": len(part), "era_only_rate": era_only,
                "mae_era_only": float(np.abs(actual - constant).mean()),
                "mae_model": float(np.abs(actual - predicted).mean()),
                "r2_vs_era_only": float(1 - ((actual - predicted) ** 2).sum()
                                        / ((actual - constant) ** 2).sum()),
                "rank_corr": float(pd.Series(predicted).corr(pd.Series(actual),
                                                             method="spearman")),
            })
    return model, pd.DataFrame(rows)


def loss_cross_validated(sample, folds=10, seed=0):
    """The secondary view: 10-fold CV R-squared, era dummies alone against era plus financials."""
    rows = sample[~sample.flag_fslic & ~sample.flag_1984_85].reset_index(drop=True)
    y = rows.loss_rate.values
    era = pd.get_dummies(rows.era, drop_first=True, dtype=float)
    lim = {c: rows[c].quantile([0.01, 0.99]).values for c in FEATURES}
    fin = pd.DataFrame({c: rows[c].clip(*lim[c]).fillna(rows[c].median()) for c in FEATURES})
    designs = {"era only": sm.add_constant(era), "era + financials": sm.add_constant(
        pd.concat([era, fin], axis=1))}
    order = np.random.default_rng(seed).permutation(len(rows))
    fold = np.empty(len(rows), dtype=int)
    fold[order] = np.arange(len(rows)) % folds
    out = {}
    for name, X in designs.items():
        predicted = np.zeros(len(rows))
        for k in range(folds):
            fit = sm.OLS(y[fold != k], X[fold != k]).fit()
            predicted[fold == k] = fit.predict(X[fold == k])
        out[name] = 1 - ((y - predicted) ** 2).sum() / ((y - y.mean()) ** 2).sum()
    return {"failures": len(rows), **{f"cv_r2_{k}": float(v) for k, v in out.items()}}


def weighted_rates(frame, predicted):
    """Count-weighted and deposit-weighted loss rates, predicted against actual."""
    deposits = frame.QBFDEP.values
    return {
        "failures": len(frame),
        "actual_cost_k": float(frame.COST.sum()),
        "predicted_cost_k": float((predicted * deposits).sum()),
        "count_weighted_actual": float(frame.loss_rate.mean()),
        "count_weighted_predicted": float(np.mean(predicted)),
        "deposit_weighted_actual": float(frame.COST.sum() / deposits.sum()),
        "deposit_weighted_predicted": float((predicted * deposits).sum() / deposits.sum()),
    }


def loss_backtest(model, sample):
    """Given failure: predicted COST (rate x deposits at failure) against booked COST, by window."""
    rows = []
    for name in WINDOWS:
        part = sample[(sample.window == name) | ((name == "test") & (sample.FAILYR > 2025))]
        if name == "train":
            part = loss_training_rows(part)
        for systemic in (True, False):
            subset = part if systemic else part[~part.flag_systemic_2023]
            rows.append({"window": name, "systemic_2023": "included" if systemic else "excluded",
                         **weighted_rates(subset, model.predict(subset))})
    return pd.DataFrame(rows)


# ------------------------------------------------------------------------------------------------
# Run-off, and the whole chain
# ------------------------------------------------------------------------------------------------

def runoff_sample(failures, prefailure):
    events, _, _ = failure_events(failures)
    events = events[events.FAILYR >= 1984]
    sample = events.merge(filing_before(events, prefailure), on="CERT", how="inner")
    sample = sample[sample.DEP > 0].copy()
    sample["runoff_ratio"] = sample.QBFDEP / sample.DEP
    sample["era"] = sample.FAILYR.map(era_of)
    sample["window"] = sample.FAILYR.map(window_of)
    sample["pre_1992"] = (sample.FAILYR <= 1991).astype(float)
    return sample, len(events)


def runoff_table(sample):
    """The ratio by era. Not clipped at 1: a quarter of failed banks grew deposits into failure."""
    def describe(x):
        return pd.Series({"failures": len(x), "mean": x.mean(), "median": x.median(),
                          "p10": x.quantile(0.1), "p90": x.quantile(0.9),
                          "share_above_1": (x > 1).mean()})
    by_era = sample.groupby("era").runoff_ratio.apply(describe).unstack()
    overall = describe(sample.runoff_ratio).to_frame("all").T
    return pd.concat([by_era, overall])


def runoff_model_comparison(sample, folds=10, seed=0):
    """Does a regression on the filing beat the median? The notebook asks; the container keeps
    the table."""
    rows = sample.reset_index(drop=True)
    y = rows.runoff_ratio.clip(0, 3).values
    lim = {c: rows[c].quantile([0.01, 0.99]).values for c in FEATURES}
    X = sm.add_constant(pd.concat([
        pd.get_dummies(rows.era, drop_first=True, dtype=float),
        pd.DataFrame({c: rows[c].clip(*lim[c]).fillna(rows[c].median()) for c in FEATURES})],
        axis=1))
    order = np.random.default_rng(seed).permutation(len(rows))
    fold = np.empty(len(rows), dtype=int)
    fold[order] = np.arange(len(rows)) % folds
    model, median, mean = np.zeros(len(rows)), np.zeros(len(rows)), np.zeros(len(rows))
    for k in range(folds):
        fit = sm.OLS(y[fold != k], X[fold != k]).fit()
        model[fold == k] = fit.predict(X[fold == k])
        median[fold == k] = np.median(y[fold != k])
        mean[fold == k] = np.mean(y[fold != k])
    cv = {"view": "10-fold CV, 1984-2026", "failures": len(rows),
          "mae_model": float(np.abs(y - model).mean()),
          "mae_median": float(np.abs(y - median).mean()),
          "r2_vs_mean": float(1 - ((y - model) ** 2).sum() / ((y - mean) ** 2).sum())}
    train, test = (rows.FAILYR <= 2007).values, (rows.FAILYR >= 2008).values
    Xf = X.drop(columns=[c for c in X.columns if c not in ("const", *FEATURES)])
    fit = sm.OLS(y[train], Xf[train]).fit()
    pred = fit.predict(Xf[test])
    const_mean, const_median = y[train].mean(), np.median(y[train])
    oot = {"view": "fit 1984-2007, test 2008-2026", "failures": int(test.sum()),
           "mae_model": float(np.abs(y[test] - pred).mean()),
           "mae_median": float(np.abs(y[test] - const_median).mean()),
           "r2_vs_mean": float(1 - ((y[test] - pred) ** 2).sum()
                               / ((y[test] - const_mean) ** 2).sum())}
    return pd.DataFrame([cv, oot])


def el_backtest(pd_model, loss_model, panel, failures, runoff_ratio):
    """Sum of PD x loss rate x deposits x run-off per window, against the COST actually booked.

    Every input is out of sample for validate and test: both models were fitted on rows whose
    horizons end by 2008, and the run-off ratio is the 1984-2007 mean.
    """
    events, _, _ = failure_events(failures)
    rows = panel[panel.drop_reason == "kept"].merge(
        events[["CERT", "COST"]], on="CERT", how="left")
    rows["pre_1992"] = (rows.horizon_year <= 1991).astype(float)
    rows["pd"] = pd_model.predict(rows)
    rows["loss_rate_hat"] = loss_model.predict(rows)
    rows["el"] = rows.pd * rows.loss_rate_hat.clip(lower=0) * rows.DEP * runoff_ratio
    rows["actual"] = np.where(rows.label == 1, rows.COST.fillna(0), 0.0)
    rows["systemic"] = rows.CERT.isin(SYSTEMIC_2023) & (rows.label == 1)
    out = []
    for name in WINDOWS:
        part = rows[rows.window == name]
        for systemic in (True, False):
            subset = part if systemic else part[~part.CERT.isin(SYSTEMIC_2023)]
            out.append({
                "window": name, "systemic_2023": "included" if systemic else "excluded",
                "bank_years": len(subset), "expected_failures": float(subset.pd.sum()),
                "actual_failures": int(subset.label.sum()),
                "expected_loss_k": float(subset.el.sum()),
                "actual_cost_k": float(subset.actual.sum()),
                "failures_without_cost": int(((subset.label == 1) & subset.COST.isna()).sum()),
            })
    table = pd.DataFrame(out)
    table["el_over_actual"] = table.expected_loss_k / table.actual_cost_k.replace(0, np.nan)
    return table


def as_of_rows(panel):
    """Every bank on the last year-end filing that was still open when its horizon began.

    The forecast is made as of the filing, so the label rule decides who is in it. A bank that
    failed on or before the filing date, or inside the embargo quarter after it, is out: no
    horizon of the label rule would count that failure. A bank that failed later is in, because
    on the filing date it was an open bank and that failure is what the PD is for. An assistance
    event never removes a bank here. It ends a bank's training rows because what happened next is
    unobserved, but Bank of America N.A. and Citibank N.A. were assisted in 2008-09 and file
    every year since.
    """
    latest = panel[panel.repdte == panel.repdte.max()]
    return latest[~latest.drop_reason.isin(["after failure", "embargo"])]


def counterparty_el(panel, failures, prefailure, institutions, runoff_ratio, top=25):
    """The last filing's banks: both models refitted on every labelled row, scored as of it."""
    labelled = panel[panel.drop_reason == "kept"]
    pd_model = fit_pd(labelled)
    sample = loss_sample(failures, prefailure)
    fit_rows = sample[~sample.flag_fslic & ~sample.flag_1984_85 & ~sample.flag_systemic_2023]
    loss_model = fit_loss(fit_rows)
    # Equity of exactly zero marks a filer with no capital of its own -- on the 2025 year-end, 15
    # branches of foreign banks and 2 CERTs with no institutions record. An equity-driven PD would
    # rank them the riskiest banks in the country, so they are left out. Negative equity is kept:
    # that is a domestic bank that is insolvent, which is exactly what the model is for.
    latest = as_of_rows(panel).copy()
    no_equity = int((latest.EQV == 0).sum())
    latest = latest[latest.EQV != 0]
    latest["pre_1992"] = 0.0
    latest["pd"] = pd_model.predict(latest)
    latest["loss_rate_hat"] = loss_model.predict(latest).clip(0, None)
    latest["deposits_k"] = latest.DEP
    latest["el_k"] = latest.pd * latest.loss_rate_hat * latest.deposits_k * runoff_ratio
    latest = latest.merge(institutions[["CERT", "NAME", "STALP"]], on="CERT", how="left")
    columns = ["CERT", "NAME", "STALP", "repdte", "ASSET", "deposits_k", "EQV", "pd",
               "loss_rate_hat", "el_k"]
    ranked = latest.sort_values("el_k", ascending=False)[columns].reset_index(drop=True)
    summary = {"banks": len(latest), "excluded_zero_equity": no_equity,
               "deposits_k": float(latest.deposits_k.sum()),
               "expected_failures": float(latest.pd.sum()),
               "expected_loss_k": float(latest.el_k.sum()),
               "as_of": str(latest.repdte.max().date()), "runoff_ratio": runoff_ratio}
    return ranked.head(top), summary, pd_model, loss_model
