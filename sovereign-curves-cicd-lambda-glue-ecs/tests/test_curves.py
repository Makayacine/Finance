"""Svensson, the bucketed FIA bootstrap, risk and the pricing-error tables.

The curves are checked against themselves: bonds priced off known Svensson parameters must give
those parameters back, an exact bootstrap must reprice every bond it was built from, and the
five key-rate DV01s must add up to the parallel one.
"""

import contextlib
import datetime as dt
import io
import math

import numpy as np
import pytest

from bond_analytics import FIA, bootstrap, dates, risk, svensson, truth
from bond_analytics.dates import Security
from bond_analytics.market import Quote

D = dt.date
SETTLE = D(2026, 10, 26)
# GSW's parameters for 2026-10-23 and the zero yields it published for that day
GSW_2026_10_23 = (5.48106339177429e-13, 4.01039120164686, 4.51159915720968, 16.1535165152315, 1.92317991734059, 15.8327487462404)
SVENY = {2: 4.6416, 10: 4.9915}


def test_formula_matches_published_yields():
    for n, y in SVENY.items():
        assert float(svensson.zero_rate(n, GSW_2026_10_23)) == pytest.approx(y, abs=5e-5)
        assert svensson.zero_rate_scalar(n, GSW_2026_10_23) == pytest.approx(float(svensson.zero_rate(n, GSW_2026_10_23)), abs=1e-12)


def test_nelson_siegel_years_have_no_second_hump():
    ns = (10.42, 2.25, -1.0, 0.0, 2.19, None)
    assert svensson.zero_rate_scalar(5.0, ns) == pytest.approx(float(svensson.zero_rate(5.0, ns)))


def universe(params, n=40):
    """Fixed-coupon notes and bonds from 1 to 30 years, priced off ``params``."""
    curve = svensson.SvenssonCurve(params)
    out = []
    for i in range(n):
        maturity = dates.add_months(D(2027, 11, 15), 9 * i, False)
        sec = Security(cusip=f"C{i:03d}", kind="bond" if i > 12 else "note", coupon=1.0 + (i % 9) * 0.5, maturity=maturity)
        bond = dates.to_relative_bond(sec, SETTLE)
        bond.set_price(bond.price(curve))
        out.append((sec, bond))
    return out


def test_fit_recovers_the_parameters_that_priced_the_bonds():
    bonds = [b for _, b in universe(GSW_2026_10_23)]
    fit = svensson.fit(bonds)
    assert fit.rmse_bp < 0.01
    for t in (1, 2, 5, 10, 20, 30):
        assert float(svensson.zero_rate(t, fit.params)) == pytest.approx(float(svensson.zero_rate(t, GSW_2026_10_23)), abs=1e-4)


def test_svensson_curve_inside_fia():
    curve = svensson.SvenssonCurve(GSW_2026_10_23)
    sec, bond = universe(GSW_2026_10_23)[10]
    by_hand = sum(c * math.exp(-float(svensson.zero_rate(t, GSW_2026_10_23)) / 100 * t) for t, c in zip(bond.get_dates(), bond.get_cashflows()))
    assert bond.price(curve) == pytest.approx(by_hand, rel=1e-12)
    up = curve.shift(1.0)  # FIA's units: percent
    assert up.discount_factor(7.0) / curve.discount_factor(7.0) == pytest.approx(math.exp(-0.01 * 7.0))
    assert isinstance(up, svensson.SvenssonCurve)


def candidates(params):
    out = []
    for sec, bond in universe(params, n=40):
        out.append(bootstrap.Candidate(sec.cusip, sec.kind, dates.year_fraction(SETTLE, sec.maturity), None, False, bond))
    curve = svensson.SvenssonCurve(params)
    for months in (1, 2, 3, 5, 7, 10):
        sec = Security(cusip=f"B{months:02d}", kind="bill", coupon=0.0, maturity=dates.add_months(SETTLE, months, False))
        bond = dates.to_relative_bond(sec, SETTLE)
        bond.set_price(bond.price(curve))
        out.append(bootstrap.Candidate(sec.cusip, "bill", dates.year_fraction(SETTLE, sec.maturity), None, False, bond))
    return out


def test_select_one_per_bucket_and_never_two_at_one_maturity():
    cands = candidates(GSW_2026_10_23)
    twin = cands[5]
    cands.append(bootstrap.Candidate("TWIN", twin.kind, twin.maturity_years, 0, False, twin.bond))
    cands.append(bootstrap.Candidate("CALL", "bond", 25.3, None, True, cands[-2].bond))
    picked = bootstrap.select(cands)
    years = [c.maturity_years for c in picked]
    assert years == sorted(years) and len(set(years)) == len(years)
    assert "TWIN" not in {c.cusip for c in picked}  # the on-the-run twin loses to the off-the-run
    assert "CALL" not in {c.cusip for c in picked}
    assert all((c.kind == "bill") == (c.maturity_years < 1) for c in picked)
    edges = bootstrap.BUCKET_EDGES
    buckets = [max(j for j in range(len(edges) - 1) if edges[j] <= t) for t in years]
    assert len(set(buckets)) == len(buckets)


def test_on_the_run_is_used_when_it_is_alone_in_its_bucket():
    c = candidates(GSW_2026_10_23)[0]
    alone = bootstrap.Candidate("OTR", c.kind, c.maturity_years, 0, False, c.bond)
    assert [p.cusip for p in bootstrap.select([alone])] == ["OTR"]


def test_fia_bootstrap_reprices_every_bond_it_was_built_from():
    picked = bootstrap.select(candidates(GSW_2026_10_23))
    with contextlib.redirect_stdout(io.StringIO()):
        curve = bootstrap.build(picked)
        for c in picked:
            assert c.bond.price(curve) == pytest.approx(c.bond.get_price(), abs=1e-5)
    assert len(curve.get_tenors()) == len(picked)


def test_key_rate_triangles_add_to_one():
    for t in (0.1, 2.0, 3.3, 7.5, 10.0, 19.9, 25.0, 40.0):
        assert sum(risk.key_rate_weight(t, k) for k in range(5)) == pytest.approx(1.0)
    assert risk.key_rate_weight(5.0, 1) == 1.0 and risk.key_rate_weight(5.0, 0) == 0.0
    assert risk.key_rate_weight(3.5, 0) == pytest.approx(0.5)


def test_risk_against_finite_differences():
    sec, bond = universe(GSW_2026_10_23)[20]
    r = risk.measure(bond, GSW_2026_10_23)
    up = bond.price(r.ytm + 0.01, compounding=2)
    down = bond.price(r.ytm - 0.01, compounding=2)
    assert r.dv01 == pytest.approx((down - up) / 2, rel=1e-4)
    assert r.modified_duration == pytest.approx(r.dv01 * 1e4 / bond.get_price(), rel=1e-9)
    assert r.convexity == pytest.approx((up + down - 2 * bond.get_price()) / bond.get_price() / 1e-8, rel=1e-3)
    assert sum(r.key_rate_dv01.values()) == pytest.approx(r.curve_dv01, rel=1e-6)
    assert abs(r.z_spread_bp) < 1e-6  # the bond was priced off this curve


def test_a_cheap_bond_has_a_positive_z_spread_and_the_same_key_rate_sum():
    sec, bond = universe(GSW_2026_10_23)[20]
    bond.set_price(bond.get_price() - 1.0)
    r = risk.measure(bond, GSW_2026_10_23)
    assert r.z_spread_bp > 0
    assert sum(r.key_rate_dv01.values()) == pytest.approx(r.curve_dv01, rel=1e-6)


def test_price_errors_are_zero_off_the_generating_curve():
    quotes = []
    for sec, bond in universe(GSW_2026_10_23, n=12):
        quotes.append(Quote(sec, D(2026, 10, 23), SETTLE, bond.get_price(), 0.0, bond.get_price(), False, None,
                            dates.year_fraction(SETTLE, sec.maturity), bond))
    df = truth.price_errors(quotes, GSW_2026_10_23)
    assert np.abs(df.error_price).max() < 1e-9 and np.abs(df.error_bp).max() < 1e-4
    table = truth.summarise(df, ["bucket", "kind"])
    assert table["n"].sum() == 12
