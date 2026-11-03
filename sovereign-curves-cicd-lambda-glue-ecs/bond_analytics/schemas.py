"""One column list per lake table: the Parquet schema every partition is written with, and the
source the Athena DDL in ``athena/create-tables.sql`` is checked against.

A column that is empty on some day (``call_date`` on a day with no callable bond, say) would
otherwise be written as Parquet's null type there and as a date elsewhere, and Athena reads a
table whose partitions disagree on a column's type as an error.
"""

from __future__ import annotations

from typing import Dict, List, Tuple

D, S, F, B = "date", "string", "double", "boolean"

TABLES: Dict[str, List[Tuple[str, str]]] = {
    "prices": [
        ("price_date", D), ("settle_date", D), ("cusip", S), ("kind", S), ("security_type", S), ("coupon", F),
        ("maturity", D), ("call_date", D), ("callable", B), ("buy", F), ("sell", F), ("clean", F), ("accrued", F),
        ("dirty", F), ("years", F), ("original_term", S), ("issue_date", D), ("first_period", S),
        ("bill_curve_gap", F), ("neighbour_gap_ticks", F), ("quality_flags", S),
    ],
    "securities": [
        ("cusip", S), ("auction_type", S), ("original_term", S), ("issue_date", D), ("dated_date", D),
        ("first_coupon_date", D), ("first_period", S), ("inflation_indexed", B), ("floating", B),
    ],
    "reference_curves": [("date", D), ("beta0", F), ("beta1", F), ("beta2", F), ("beta3", F), ("tau1", F), ("tau2", F)]
    + [(f"sveny{n:02d}", F) for n in range(1, 31)],
    "funding": [("date", D), ("rate_type", S), ("percent_rate", F), ("volume_bn", F)],
    "curves": [("price_date", D), ("curve", S), ("tenor", F), ("zero_rate", F), ("par_yield", F), ("discount_factor", F)],
    "curve_params": [
        ("price_date", D), ("curve", S), ("beta0", F), ("beta1", F), ("beta2", F), ("beta3", F), ("tau1", F),
        ("tau2", F), ("rmse_bp", F), ("n_bonds", F), ("params_at_bound", S), ("seconds", F), ("funding_rate", F),
        ("funding_source", S),
    ],
    "fit_errors": [
        ("price_date", D), ("cusip", S), ("kind", S), ("coupon", F), ("maturity", D), ("years", F),
        ("original_term", S), ("issue_date", D), ("run", S), ("callable", B), ("clean", F), ("accrued", F),
        ("dirty", F), ("model_dirty", F), ("error_price", F), ("ytm", F), ("error_bp", F), ("bucket", S),
        ("curve", S), ("in_fit", B), ("params_at_bound", S),
    ],
    "risk": [
        ("price_date", D), ("cusip", S), ("kind", S), ("coupon", F), ("maturity", D), ("years", F), ("run", S),
        ("dirty", F), ("ytm", F), ("dv01", F), ("modified_duration", F), ("convexity", F), ("z_spread_bp", F),
        ("curve_dv01", F), ("carry_30d", F), ("krd_2y", F), ("krd_5y", F), ("krd_10y", F), ("krd_20y", F),
        ("krd_30y", F),
    ],
}


def columns(table: str) -> List[str]:
    return [c for c, _ in TABLES[table]]


def arrow_schema(table: str):
    import pyarrow as pa

    types = {D: pa.date32(), S: pa.string(), F: pa.float64(), B: pa.bool_()}
    return pa.schema([(c, types[t]) for c, t in TABLES[table]])
