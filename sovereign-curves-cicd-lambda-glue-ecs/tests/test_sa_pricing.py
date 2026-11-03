"""The JSE yield-to-price formula, FIA's bootstrap on it, and the monthly check against OECD and IMF.

The formula is held to the National Treasury's own numbers: each bond's initial issue price in
its debt schedule, against the clearing yield of the auction that settled on the issue date.
"""

import datetime as dt

import pandas as pd
import pytest

from bond_analytics import jse, sa_curve

D = dt.date


def bond(code, coupon, maturity, months, days):
    return jse.Bond(code, coupon, maturity, months, days)


R213 = bond("R213", 7.0, D(2031, 2, 28), (2, 8), (28, 31))
R214 = bond("R214", 6.5, D(2041, 2, 28), (2, 8), (28, 31))
R2030 = bond("R2030", 8.0, D(2030, 1, 31), (1, 7), (31, 31))
R2037 = bond("R2037", 8.5, D(2037, 1, 31), (1, 7), (31, 31))
R2044 = bond("R2044", 8.75, D(2044, 1, 31), (1, 7), (31, 31))
R2035 = bond("R2035", 8.875, D(2035, 2, 28), (2, 8), (28, 31))
R2038 = bond("R2038", 10.875, D(2038, 3, 31), (3, 9), (31, 30))
R2039 = bond("R2039", 9.875, D(2039, 3, 31), (3, 9), (31, 30))
R2048 = bond("R2048", 8.75, D(2048, 2, 28), (2, 8), (28, 31))
R209 = bond("R209", 6.25, D(2036, 3, 31), (3, 9), (31, 30))
R2023 = bond("R2023", 7.75, D(2023, 2, 28), (2, 8), (28, 31))


# The schedule's issue price, the first auction's settlement and clearing yield, and which price the
# Treasury published: clean for the older issues, all-in for most from 2014.
ISSUES = [
    (R213, D(2010, 5, 28), 9.05, 80.922, "clean"),
    (R214, D(2010, 6, 4), 8.82, 75.517, "clean"),
    (R2037, D(2013, 7, 19), 8.82, 96.875, "clean"),
    (R2030, D(2013, 10, 4), 8.54, 95.261, "clean"),
    (R2044, D(2014, 7, 18), 9.065, 100.832, "all_in"),
    (R2035, D(2015, 7, 17), 8.82, 103.843, "all_in"),
    (R2038, D(2024, 9, 13), 10.905, 104.710, "all_in"),
    (R2039, D(2025, 10, 3), 9.905, 99.778, "clean"),
]


@pytest.mark.parametrize("b, settle, y, published, kind", ISSUES, ids=[i[0].code for i in ISSUES])
def test_the_first_auctions_yield_gives_the_treasurys_issue_price(b, settle, y, published, kind):
    p = jse.price(b, settle, y)
    assert getattr(p, kind) == pytest.approx(published, abs=1e-3)


def test_r2030s_issue_price_settles_its_coupon_cycle():
    """On the schedule's 28/02 and 31/08 the accrued interest would be 34 days, not 65, and the
    clean price would miss the Treasury's 95.261 by 0.08 rather than 0.0002."""
    jan_jul = jse.price(R2030, D(2013, 10, 4), 8.54)
    feb_aug = jse.price(bond("R2030", 8.0, D(2030, 1, 31), (2, 8), (28, 31)), D(2013, 10, 4), 8.54)
    assert jan_jul.accrued == pytest.approx(65 * 8.0 / 365, abs=1e-5)
    assert feb_aug.accrued == pytest.approx(34 * 8.0 / 365, abs=1e-5)
    assert abs(feb_aug.clean - 95.261) > 0.05 > 0.001 > abs(jan_jul.clean - 95.261)


def test_the_books_close_ten_days_before_the_coupon():
    cum = jse.price(R2048, D(2012, 8, 20), 8.3)  # coupon 31 August, books closed from the 21st
    ex = jse.price(R2048, D(2012, 8, 21), 8.3)
    assert cum.cum and not ex.cum
    assert cum.accrued == pytest.approx((D(2012, 8, 20) - D(2012, 2, 28)).days * 8.75 / 365, abs=1e-5)
    assert ex.accrued == pytest.approx(-10 * 8.75 / 365, abs=1e-5)
    # A day apart, the all-in price drops by the half coupon's value ten days out, 4.34. The clean
    # price moves 0.07: actual/365 accrues 184 days of a 4.375 coupon as 4.41.
    assert cum.all_in - ex.all_in == pytest.approx(4.34, abs=0.01)
    assert ex.clean - cum.clean == pytest.approx(0.07, abs=0.005)


def test_an_ex_coupon_buyer_gets_no_next_coupon():
    flows = jse.cash_flows(R2048, D(2012, 8, 24))
    assert flows[0] == (D(2013, 2, 28), 4.375) and flows[-1] == (D(2048, 2, 28), 104.375)
    assert jse.cash_flows(R2048, D(2012, 8, 20))[0] == (D(2012, 8, 31), 4.375)


def test_a_coupon_on_the_28th_of_february_stays_there_in_a_leap_year():
    assert jse.coupon_dates(R2048, D(2023, 12, 1))[:2] == [D(2024, 2, 28), D(2024, 8, 31)]


def test_the_last_coupon_period_is_refused():
    with pytest.raises(ValueError, match="last coupon period"):
        jse.price(R2023, D(2022, 12, 1), 7.0)


def test_dv01_duration_and_convexity_are_the_formulas_own():
    y, settle = 9.065, D(2014, 7, 18)
    p = jse.price(R2044, settle, y, rounded=False).all_in
    d01 = jse.dv01(R2044, settle, y)
    assert d01 == pytest.approx((jse.price(R2044, settle, y - 0.5, rounded=False).all_in
                                 - jse.price(R2044, settle, y + 0.5, rounded=False).all_in) / 100, rel=5e-3)
    assert 9.0 < d01 * 1e4 / p < 11.0  # a 30-year bond at 9%


def test_fia_reprices_the_all_in_price_on_the_jse_cash_flows():
    p = jse.price(R2044, D(2014, 7, 18), 9.065)
    rb = jse.to_relative_bond(R2044, D(2014, 7, 18), p.all_in)
    assert rb.get_price() == p.all_in
    assert rb.get_maturity() == pytest.approx((D(2044, 1, 31) - D(2014, 7, 18)).days / 365)
    assert abs(rb.YTM(p.all_in, compounding=2) - 9.065) < 0.05  # FIA's own yield is near the JSE's


# --- one auction's curve, and the monthly check -----------------------------------------------

STATIC = {
    b.code: {"bond_code": b.code, "coupon": b.coupon, "pricing_maturity": b.maturity,
             "coupon_months": ",".join(map(str, b.coupon_months)), "coupon_days": ",".join(map(str, b.coupon_days))}
    for b in (R2030, R209, R2048, R2037, R213)
}


def row(day, code, y, settle=None, flags=""):
    return {"auction_date": day, "settle_date": settle or day + dt.timedelta(days=3), "bond_code": code,
            "clearing_yield": y, "quality_flags": flags}


def test_the_bootstrap_reprices_every_bond_it_is_built_from():
    day = D(2026, 9, 1)
    rows = [row(day, "R2030", 8.1), row(day, "R209", 9.4), row(day, "R2048", 10.2)]
    priced = sa_curve.price_rows(rows, STATIC)
    curve = sa_curve.build(priced)
    for r in priced:
        assert r["_bond"].price(curve) == pytest.approx(r["all_in"], abs=1e-7)
    grid = {g["tenor"]: g for g in sa_curve.curve_rows(day, priced, curve)}
    assert grid[2.0]["extrapolated"] and not grid[10.0]["extrapolated"] and not grid[20.0]["extrapolated"]
    assert grid[3.0]["n_bonds"] == 3.0 and grid[3.0]["shortest"] == pytest.approx(priced[0]["years"])
    assert grid[10.0]["zero_rate"] > grid[5.0]["zero_rate"]


def test_a_flagged_yield_never_reaches_the_curve():
    day = D(2026, 9, 1)
    rows = [row(day, "R2030", 8.1), row(day, "R209", 1.85, flags="clearing_outside_bids")]
    prices, grid = sa_curve.day_tables(day, rows, STATIC)
    assert [p["bond_code"] for p in prices] == ["R2030"] and all("_bond" not in p for p in prices)
    assert sa_curve.day_tables(day, rows[1:], STATIC) == ([], [])


def test_one_curve_needs_one_settlement_date():
    day = D(2013, 3, 19)
    priced = sa_curve.price_rows([row(day, "R209", 8.26, D(2013, 3, 25)), row(day, "R2048", 8.47, D(2013, 3, 22))], STATIC)
    with pytest.raises(ValueError, match="one settlement date"):
        sa_curve.build(priced)


def test_the_monthly_check_averages_inside_the_knots_only():
    curves = pd.DataFrame([
        {"auction_date": D(2026, 9, 1), "tenor": 10.0, "par_yield": 8.60, "zero_rate": 8.7, "extrapolated": False},
        {"auction_date": D(2026, 9, 8), "tenor": 10.0, "par_yield": 8.80, "zero_rate": 8.9, "extrapolated": False},
        {"auction_date": D(2026, 9, 15), "tenor": 10.0, "par_yield": 7.00, "zero_rate": 7.0, "extrapolated": True},
        {"auction_date": D(2026, 10, 6), "tenor": 10.0, "par_yield": 7.00, "zero_rate": 7.0, "extrapolated": True},
        {"auction_date": D(2026, 9, 1), "tenor": 5.0, "par_yield": 1.0, "zero_rate": 1.0, "extrapolated": False},
    ])
    reference = pd.DataFrame([{"date": D(2026, 9, 1), "series": "oecd_irlt", "percent": 9.0},
                              {"date": D(2026, 9, 1), "series": "imf_ifs", "percent": 8.9}])
    check = sa_curve.crosscheck(curves, reference).set_index("month")
    sep = check.loc[D(2026, 9, 1)]
    assert (sep["auction_dates"], sep["inside_knots"]) == (3.0, 2.0)
    assert sep["par_10y"] == pytest.approx(8.70) and sep["par_minus_oecd_bp"] == pytest.approx(-30.0)
    assert sep["par_minus_imf_bp"] == pytest.approx(-20.0)
    assert check.loc[D(2026, 10, 1)]["inside_knots"] == 0.0 and pd.isna(check.loc[D(2026, 10, 1)]["par_10y"])
    s = sa_curve.summary(check.reset_index())
    assert s["oecd_months"] == 1.0 and s["oecd_mean_bp"] == pytest.approx(-30.0)
