"""The characteristics a company-year is scored on, from its latest accounts and the register.

Ratios only from figures in the same statement, sizes in euros, the firm's age from the register,
and which fiscal year the accounts are for. A ratio whose base is zero or negative is not a
number but a fact -- an empty shell has no assets to divide by -- so it is kept as its own bin
(``zero base``) rather than imputed, and a statement with no balance sheet or income statement
attached gets a ``none`` bin of its own.
"""

import numpy as np
import pandas as pd

# name -> (numerator, denominator)
RATIOS = {
    "equity_ratio": ("equity", "total_assets"),
    "current_ratio": ("total_current_assets", "current_liabilities"),
    "return_on_assets": ("net_income", "total_assets"),
    "cash_ratio": ("cash", "total_assets"),
    "asset_turnover": ("net_turnover", "total_assets"),
    "interest_burden": ("interest_expenses", "liabilities"),
}
SIZES = ("total_assets", "net_turnover", "employees")
NUMERIC = tuple(RATIOS) + SIZES + ("firm_age",)
CATEGORICAL = ("accounts_lag", "legal_form")
FEATURES = NUMERIC + CATEGORICAL

NONE, ZERO_BASE = "none", "zero base"


def build(rows):
    """Add every characteristic to the scored rows; rows with no accounts get none of them."""
    out = rows.copy()
    out["liabilities"] = out.current_liabilities.fillna(0) + out.non_current_liabilities.fillna(0)
    out.loc[out.total_assets.isna(), "liabilities"] = np.nan
    for name, (top, base) in RATIOS.items():
        numerator, denominator = out[top].astype(float), out[base].astype(float)
        ratio = numerator / denominator.where(denominator > 0)
        out[name] = ratio
        out[name + "_state"] = np.where(
            denominator.isna() | numerator.isna(), NONE,
            np.where(denominator <= 0, ZERO_BASE, "value"))
    for name in SIZES:
        out[name + "_state"] = np.where(out[name].isna(), NONE, "value")
    obs = pd.to_datetime(out.observation_year.astype(str) + "-10-01")
    out["firm_age"] = (obs - pd.to_datetime(out.registered)).dt.days / 365.25
    out["firm_age_state"] = np.where(out.firm_age.isna(), NONE, "value")
    out["legal_form"] = out["type"].where(out["type"].isin(["SIA", "AS", "KB"]), "other")
    return out


def benchmark(frame):
    """Equity over total assets, as a risk score (higher is riskier): the plain benchmark.

    A company with no assets to divide by sits at the median, neither safe nor risky, so the
    benchmark is judged on the ratio alone and not on a choice about where to put the shells.
    """
    ratio = frame.equity.astype(float) / frame.total_assets.where(frame.total_assets > 0)
    return -ratio.fillna(ratio.median())
