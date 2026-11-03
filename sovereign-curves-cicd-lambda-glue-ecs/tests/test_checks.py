"""The checks that guard the numbers: the quote flag, the bound flag and the two error tables."""

import datetime as dt
import math

import numpy as np
import pandas as pd
import pytest

from bond_analytics import dates, quality, svensson, truth
from bond_analytics.dates import Security

D = dt.date
SETTLE = D(2026, 10, 26)


def test_bill_curve_is_log_linear_between_bills():
    curve = quality.BillCurve([(0.1, 99.6), (0.3, 98.8)])
    mid = curve.discount(0.2)
    assert mid == pytest.approx(math.sqrt(0.996 * 0.988))
    assert curve.discount(0.05) is None and curve.discount(0.31) is None


def day_rows(odd_ticks):
    rate = 0.04
    rows = []
    for k in range(1, 30):
        sec = Security(f"B{k}", "bill", 0.0, SETTLE + dt.timedelta(days=7 * k))
        rows.append({"security": sec, "end_of_day": 100 * math.exp(-rate * dates.year_fraction(SETTLE, sec.maturity))})
    for i, (cpn, mat) in enumerate([(2.0, D(2026, 11, 15)), (4.625, D(2026, 11, 15)), (3.0, D(2026, 11, 30)), (6.5, D(2026, 11, 15))]):
        sec = Security(f"N{i}", "note", cpn, mat)
        value = sum(a * math.exp(-rate * dates.year_fraction(SETTLE, d)) for d, a in dates.cash_flows(sec, SETTLE))
        clean = round((value - dates.accrued_interest(sec, SETTLE)) * 32) / 32 + (odd_ticks / 32 if i == 3 else 0)
        rows.append({"security": sec, "end_of_day": clean})
    for r in rows:
        r["dirty"] = r["end_of_day"] + dates.accrued_interest(r["security"], SETTLE)
    return rows


def test_a_quote_seven_ticks_from_its_neighbours_is_flagged_and_kept():
    rows = day_rows(-7)
    before = [r["end_of_day"] for r in rows]
    quality.flag_quotes(rows, SETTLE)
    assert [r["end_of_day"] for r in rows] == before
    flagged = [r["security"].cusip for r in rows if "off_neighbours" in r["flags"]]
    assert flagged == ["N3"]
    assert rows[-1]["neighbour_gap_ticks"] == pytest.approx(-7, abs=1)
    assert all(r["bill_curve_gap"] is None for r in rows if r["security"].kind == "bill")


def test_a_common_basis_is_not_a_flag():
    rows = day_rows(0)
    for r in rows:  # every note three 32nds cheap to the bills: a basis, not a bad quote
        if r["security"].kind == "note":
            r["dirty"] -= 3 / 32
    quality.flag_quotes(rows, SETTLE)
    assert not any("off_neighbours" in r["flags"] for r in rows)
    assert all(r["bill_curve_gap"] == pytest.approx(-3 / 32, abs=1 / 32) for r in rows if r["security"].kind == "note")


def test_a_lone_quote_has_no_neighbours_to_be_judged_by():
    rows = day_rows(-7)
    rows = [r for r in rows if r["security"].kind == "bill" or r["security"].cusip in ("N2", "N3")]
    quality.flag_quotes(rows, SETTLE)
    assert all(r["neighbour_gap_ticks"] is None for r in rows)


def test_a_fit_on_a_bound_says_so():
    assert svensson.params_at_bound(svensson.LOWER + 1.0) == ()
    x = svensson.LOWER + 1.0
    x[0] = 0.0
    x[3] = svensson.UPPER[3]
    assert svensson.params_at_bound(x) == ("beta0", "beta3")


def errors_frame():
    rng = np.random.default_rng(7)
    rows = []
    for bucket, run in [("2-3y", "on-the-run"), ("2-3y", "off-the-run"), ("2-3y", "off-the-run"), ("0-1y", "off-the-run"),
                        ("5-7y", "first off-the-run"), ("5-7y", "on-the-run"), ("5-7y", "off-the-run")] * 6:
        cpn = float(rng.uniform(0.5, 6.0))
        rows.append({"price_date": D(2026, 10, 23), "bucket": bucket, "run": run, "kind": "note", "coupon": cpn,
                     "error_bp": 1.5 * cpn + {"2-3y": 3.0, "0-1y": -20.0, "5-7y": 0.0}[bucket] + (8.0 if run == "on-the-run" else 0.0),
                     "error_price": 0.01 * cpn})
    return pd.DataFrame(rows)


def test_run_status_is_compared_inside_buckets_with_an_on_the_run_issue():
    table = truth.run_status_within_buckets(errors_frame())
    buckets = sorted({b for _, b in table.index})
    assert buckets == ["2-3y", "5-7y"]  # 0-1y has no on-the-run issue, so it cannot tilt the comparison
    assert list(table.columns[:3]) == ["on-the-run", "first off-the-run", "off-the-run"]


def test_coupon_slope_within_buckets_leaves_out_on_the_run():
    out = truth.coupon_slope(errors_frame())
    assert out["slope"] == pytest.approx(1.5, abs=1e-9)  # bucket levels absorbed, on-the-run premium left out
    assert out["n"] == 30
    assert truth.coupon_slope(errors_frame(), "error_price")["slope"] == pytest.approx(0.01)


def test_tables_apply_the_three_month_rule():
    df = pd.DataFrame({"years": [0.2, 0.25, 3.0], "callable": [False, False, True]})
    assert list(truth.table_rows(df).index) == [1]
