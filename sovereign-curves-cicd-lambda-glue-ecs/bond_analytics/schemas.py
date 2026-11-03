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
    # South Africa: the National Treasury's auctions and debt schedule, the OECD and IMF series,
    # and what the engine makes of them.
    "sa_bonds": [
        ("bond_code", S), ("coupon", F), ("issue_date", D), ("issue_price", F), ("interest_dates_listed", S),
        ("coupon_months", S), ("coupon_days", S), ("redemptions", S), ("pricing_maturity", D), ("legs", F),
        ("nominal_rand", F), ("schedule_date", D), ("quality_flags", S), ("source", S),
    ],
    "sa_auctions": [
        ("auction_date", D), ("settle_date", D), ("bond_code", S), ("bond_label", S), ("coupon_quoted", F),
        ("redemption_year_quoted", F), ("offered_rand", F), ("allocated_rand", F), ("non_comps_rand", F),
        ("bids_received_rand", F), ("number_of_bids", F), ("clearing_yield", F), ("best_bid", F), ("worst_bid", F),
        ("bid_to_cover", F), ("yield_unit", S), ("source_file", S), ("sheet", S), ("quality_flags", S), ("source", S),
    ],
    "sa_reference": [("date", D), ("series", S), ("percent", F), ("source", S)],
    "sa_prices": [
        ("auction_date", D), ("settle_date", D), ("bond_code", S), ("coupon", F), ("maturity", D), ("years", F),
        ("clearing_yield", F), ("cum", B), ("all_in", F), ("accrued", F), ("clean", F), ("dv01", F),
        ("modified_duration", F), ("convexity", F), ("fia_ytm", F), ("source", S),
    ],
    "sa_curves": [
        ("auction_date", D), ("tenor", F), ("zero_rate", F), ("par_yield", F), ("discount_factor", F),
        ("extrapolated", B), ("n_bonds", F), ("shortest", F), ("longest", F),
    ],
    "sa_crosscheck": [
        ("month", D), ("auction_dates", F), ("inside_knots", F), ("par_10y", F), ("zero_10y", F), ("oecd_irlt", F),
        ("imf_ifs", F), ("par_minus_oecd_bp", F), ("par_minus_imf_bp", F),
    ],
}


def columns(table: str) -> List[str]:
    return [c for c, _ in TABLES[table]]


def to_csv(table: str, rows: List[dict]) -> str:
    """A table's rows as CSV text in its column order: ISO dates, empty for a missing value."""
    import csv
    import io

    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(columns(table))
    for r in rows:
        out = []
        for c, t in TABLES[table]:
            v = r.get(c)
            if v is None or (t == F and v != v):
                out.append("")
            elif t == D:
                out.append(v.isoformat())
            elif t == B:
                out.append("true" if v else "false")
            else:
                out.append(repr(float(v)) if t == F else str(v))
        w.writerow(out)
    return buf.getvalue()


def from_csv(table: str, text: str) -> List[dict]:
    """``to_csv`` read back, each value in its column's type."""
    import csv
    import datetime as dt
    import io

    types = dict(TABLES[table])
    conv = {D: dt.date.fromisoformat, F: float, B: lambda s: s == "true", S: str}
    rows = []
    for rec in csv.DictReader(io.StringIO(text)):
        rows.append({c: (None if rec[c] == "" else conv[types[c]](rec[c])) for c in columns(table)})
    return rows


def arrow_schema(table: str):
    import pyarrow as pa

    types = {D: pa.date32(), S: pa.string(), F: pa.float64(), B: pa.bool_()}
    return pa.schema([(c, types[t]) for c, t in TABLES[table]])
