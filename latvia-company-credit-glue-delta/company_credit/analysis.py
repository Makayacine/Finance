"""From company-year rows to every figure the README quotes: fit, windows, years, sensitivities.

The model job calls ``run`` on the rows it built by time travel and writes the tables it
returns; the notebook calls the same function on the same rows. Nothing here reads a file.
"""

import numpy as np
import pandas as pd

from . import config
from . import features as feat
from . import metrics, scorecard, woe

WINDOWS = {"train": config.TRAIN, "validate": config.VALIDATE, "test": config.TEST}
# Liabilities bands: each year's scored companies ranked by total liabilities, largest first.
BANDS = ((100, "top 100"), (1_000, "101-1,000"), (10_000, "1,001-10,000"), (None, "the rest"))


def window_of(years):
    out = pd.Series("", index=years.index, dtype=object)
    for name, (first, last) in WINDOWS.items():
        out[years.between(first, last)] = name
    return out


def prepare(rows):
    """Characteristics on every covered row; uncovered rows are kept for the coverage figures."""
    rows = rows.copy()
    rows["window"] = window_of(rows.observation_year)
    covered = feat.build(rows[rows.covered])
    return rows, covered


def coverage(rows):
    """Per year: the population, how much of it the PD covers, and how many of its defaults."""
    table = rows.groupby("observation_year").agg(
        live=("regcode", "size"), covered=("covered", "sum"), defaults=("y", "sum"),
        covered_defaults=("y", lambda s: int(s[rows.loc[s.index, "covered"]].sum())),
        previous_year_accounts=("accounts_lag", lambda s: int((s == "previous").sum())),
    )
    table["covered_share"] = table.covered / table.live
    table["defaulter_coverage"] = table.covered_defaults / table.defaults
    return table.reset_index()


def windows_table(scored, score="score", y="y"):
    out = []
    for name in WINDOWS:
        part = scored[scored.window == name]
        model = metrics.discrimination(part[y], part[score])
        bench = metrics.discrimination(part[y], part["benchmark"])
        out.append({"window": name, **model, "benchmark_auc": bench["auc"],
                    "benchmark_low": bench["auc_low"], "benchmark_high": bench["auc_high"],
                    "pd_ttc": part.pd_ttc.mean(), "pd_validation": part.pd_validation.mean()})
    return pd.DataFrame(out)


def years_table(scored, edges, reference):
    out = []
    for year, part in scored.groupby("observation_year"):
        d = metrics.discrimination(part.y, part.score)
        out.append({
            "observation_year": year, "n": d["n"], "defaults": d["defaults"],
            "observed": d["default_rate"], "auc": d["auc"], "auc_low": d["auc_low"],
            "auc_high": d["auc_high"], "benchmark_auc": metrics.auc(part.y, part.benchmark),
            "auc_insolvency_only": metrics.auc(part.y_insolvency, part.score),
            "pd_ttc": part.pd_ttc.mean(), "pd_validation": part.pd_validation.mean(),
            "expected_ttc": part.pd_ttc.sum(), "expected_validation": part.pd_validation.sum(),
            "psi": metrics.psi(reference, part.score, edges),
        })
    table = pd.DataFrame(out)
    table["oe_ttc"] = table.defaults / table.expected_ttc
    table["oe_validation"] = table.defaults / table.expected_validation
    return table


def score_rows(card, frame):
    frame = frame.copy()
    frame["score"] = card.linear(frame)
    frame["pd_ttc"] = card.pd(frame, "ttc")
    frame["pd_validation"] = card.pd(frame, "validation")
    frame["benchmark"] = feat.benchmark(frame)
    return frame


def run(rows, live_rows=None, bulk_rows=None):
    """Fit on the training years, calibrate on validation, report everything."""
    rows, covered = prepare(rows)
    train = covered[covered.window == "train"]
    card = scorecard.fit(train)
    card.calibrate(covered[covered.window == "validate"])
    scored = score_rows(card, covered)
    reference = scored[scored.window == "train"].score
    edges = metrics.decile_edges(reference)

    out = {
        "card": card, "scored": scored,
        "coverage": coverage(rows),
        "windows": windows_table(scored),
        "years": years_table(scored, edges, reference),
        "coefficients": card.result.table(),
        "iv": card.iv_table(),
        "points": card.points(),
        "dropped": pd.DataFrame(card.dropped, columns=["characteristic", "reason"]),
        "level": pd.DataFrame([{"validation_shift": card.shift,
                                "ttc_train_mean": scored[scored.window == "train"].pd_ttc.mean(),
                                "train_rate": train.y.mean()}]),
    }

    # The insolvency-only label, same score: does the ranking hold when protection is not default?
    out["insolvency_only"] = pd.DataFrame([
        {"window": name, **metrics.discrimination(part.y_insolvency, part.score)}
        for name, part in scored.groupby("window")
    ])

    # Trained without 2009-10, whose accounts cover 15.0% and 29.4% of live companies.
    first, last = config.TRAIN_SENSITIVITY
    alt = scorecard.fit(covered[covered.observation_year.between(first, last)])
    alt.calibrate(covered[covered.window == "validate"])
    alt_scored = score_rows(alt, covered)
    out["train_2011"] = pd.DataFrame([
        {"window": name, **metrics.discrimination(part.y, part.score),
         "pd_ttc": part.pd_ttc.mean(), "pd_validation": part.pd_validation.mean()}
        for name, part in alt_scored.groupby("window")
    ])

    # The same fit with late filing left to the IV screen, which drops it.
    plain = scorecard.fit(train, forced=())
    plain.calibrate(covered[covered.window == "validate"])
    plain_scored = score_rows(plain, covered)
    out["without_accounts_lag"] = pd.DataFrame([
        {"window": name, **metrics.discrimination(part.y, part.score)}
        for name, part in plain_scored.groupby("window")
    ])
    out["accounts_lag"] = pd.DataFrame([
        {"window": name, "iv": woe.woe_table(part.accounts_lag, part.y).iv.sum(),
         "previous_share": (part.accounts_lag == "previous").mean(),
         "rate_previous": part[part.accounts_lag == "previous"].y.mean(),
         "rate_current": part[part.accounts_lag == "current"].y.mean()}
        for name, part in covered.groupby("window")
    ])

    out["liabilities_bands"] = by_liabilities(scored[scored.window == "test"])
    out["liabilities_bands_train"] = by_liabilities(scored[scored.window == "train"])

    if bulk_rows is not None:
        out["bulk_dating"] = bulk_dating_cost(card, rows, bulk_rows)
    if live_rows is not None:
        live, book = live_book(card, live_rows)
        out["live"], out["live_book"] = live, book
        out["live_bands"] = by_liabilities(live)
    return out


def liabilities_band(frame):
    """Each row's band by total liabilities, ranked within its observation year, largest first.
    A statement with no balance sheet has no liabilities and falls in the rest."""
    rank = frame.groupby("observation_year").liabilities.rank(method="first", ascending=False)
    band = pd.Series(BANDS[-1][1], index=frame.index, dtype=object)
    lower = 0
    for upper, name in BANDS[:-1]:
        band[(rank > lower) & (rank <= upper)] = name
        lower = upper
    return band


def by_liabilities(scored):
    """Observed against predicted by liabilities band, and what each band's accounts look like.

    The scorecard was fitted on a population whose typical member is a company of one or two
    people, so the question is whether it carries to the few companies that hold most of the
    liabilities. With labels (the windows), each band's observed default rate is set against its
    mean PD at both levels; without them (the live book), only the PD and the exposure share.
    The profile is the median of each ratio over the band's company-years, where it has a base.
    """
    frame = scored.assign(band=liabilities_band(scored))
    weights = frame.liabilities.clip(lower=0).fillna(0)
    labelled = "y" in frame and frame.y.notna().all()
    out = []
    for name in [name for _, name in BANDS] + ["all"]:
        part = frame if name == "all" else frame[frame.band == name]
        employees = part.employees.dropna()
        row = {
            "band": name, "company_years": len(part),
            "liabilities_share": weights[part.index].sum() / weights.sum(),
            "pd_ttc": part.pd_ttc.mean(), "pd_validation": part.pd_validation.mean(),
            "median_employees": employees.median(),
            "one_employee_or_none": (employees <= 1).mean(),
            "median_cash_ratio": part.cash_ratio[part.cash_ratio_state == "value"].median(),
            "median_current_ratio":
                part.current_ratio[part.current_ratio_state == "value"].median(),
            "median_equity_ratio":
                part.equity_ratio[part.equity_ratio_state == "value"].median(),
        }
        if labelled:
            row.update({
                "defaults": int(part.y.sum()), "observed": part.y.mean(),
                "oe_ttc": part.y.sum() / part.pd_ttc.sum(),
                "oe_validation": part.y.sum() / part.pd_validation.sum(),
            })
        out.append(row)
    return pd.DataFrame(out)


def bulk_dating_cost(card, rows, bulk_rows):
    """The test years scored twice: with real filing dates, and with every statement dated the
    bulk way, nine months after its period end. The gap is what the bulk dating does to the
    training years, where the real dates are not in the file.

    A late filer is a company-year the re-dating changes: one whose latest accounts on the
    observation date were filed after it, so that with real dates it is scored on older accounts
    or not at all.
    """
    real = rows[rows.window == "test"]
    bulk = bulk_rows[bulk_rows.observation_year.between(*config.TEST)]
    real_s = score_rows(card, prepare(real)[1])
    bulk_s = score_rows(card, prepare(bulk)[1])
    keys = ["regcode", "observation_year"]
    both = bulk.merge(real[keys + ["statement_id"]], on=keys, how="left", suffixes=("", "_real"))
    late = both.covered & (both.statement_id != both.statement_id_real)
    row = {
        "real_rows": len(real_s), "real_defaults": int(real_s.y.sum()),
        "real_auc": metrics.auc(real_s.y, real_s.score),
        "real_defaulter_coverage": real_s.y.sum() / real.y.sum(),
        "bulk_rows": len(bulk_s), "bulk_defaults": int(bulk_s.y.sum()),
        "bulk_auc": metrics.auc(bulk_s.y, bulk_s.score),
        "bulk_defaulter_coverage": bulk_s.y.sum() / bulk.y.sum(),
        "added_rows": int((both.covered & both.statement_id_real.isna()).sum()),
        "late_filers": int(late.sum()), "late_defaults": int(both[late].y.sum()),
        "late_rate": both[late].y.mean(), "on_time_rate": both[both.covered & ~late].y.mean(),
    }
    return pd.DataFrame([row])


def live_book(card, live_rows):
    """Every live company on the latest observation date, scored; the horizon is still open."""
    rows = live_rows.copy()
    covered = feat.build(rows[rows.covered])
    scored = score_rows(card, covered)
    scored["exposure_proxy"] = scored.liabilities
    weights = scored.exposure_proxy.clip(lower=0).fillna(0)
    summary = {
        "observation_year": int(rows.observation_year.iloc[0]),
        "live": len(rows), "scored": len(scored), "covered_share": len(scored) / len(rows),
        "previous_year_accounts": int((scored.accounts_lag == "previous").sum()),
        "pd_ttc_mean": scored.pd_ttc.mean(), "pd_validation_mean": scored.pd_validation.mean(),
        "expected_defaults_ttc": scored.pd_ttc.sum(),
        "expected_defaults_validation": scored.pd_validation.sum(),
        "exposure_proxy_eur": float(weights.sum()),
        "pd_validation_exposure_weighted": float((scored.pd_validation * weights).sum() / weights.sum()),
    }
    return scored, pd.DataFrame([summary])
