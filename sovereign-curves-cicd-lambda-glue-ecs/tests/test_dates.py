"""The date adapter: business days, coupon schedules, accrued interest and FIA bonds.

Every expected figure is worked by hand from the rule it tests, so a change in the code that
moves a figure fails here rather than drifting through to the curves.
"""

import datetime as dt

import pytest

from bond_analytics import dates
from bond_analytics.dates import Security

D = dt.date


def note(coupon, maturity, **kw):
    return Security(cusip="TEST", kind="note", coupon=coupon, maturity=maturity, **kw)


def test_fed_holidays_2026():
    h = dates.fed_holidays(2026)
    assert D(2026, 10, 12) in h  # Columbus Day
    assert D(2026, 11, 11) in h  # Veterans Day, a Wednesday
    assert D(2026, 11, 26) in h  # Thanksgiving
    assert D(2026, 6, 19) in h  # Juneteenth, a Friday
    assert D(2026, 7, 3) not in h  # 4 July is a Saturday: the Reserve Banks open the Friday before


def test_sunday_holiday_moves_to_monday_and_juneteenth_starts_in_2022():
    assert D(2022, 6, 20) in dates.fed_holidays(2022)
    assert not any(d.month == 6 and d.day in (19, 20) for d in dates.fed_holidays(2021))
    assert D(2023, 1, 2) in dates.fed_holidays(2023)  # 1 January 2023 was a Sunday


@pytest.mark.parametrize(
    "price_date, settle",
    [
        (D(2026, 10, 30), D(2026, 11, 2)),  # Friday to Monday
        (D(2026, 10, 9), D(2026, 10, 13)),  # over Columbus Day
        (D(2026, 11, 25), D(2026, 11, 27)),  # over Thanksgiving
        (D(2026, 10, 14), D(2026, 10, 15)),
    ],
)
def test_settlement_is_the_next_business_day(price_date, settle):
    assert dates.settlement_date(price_date) == settle


def test_month_end_rule():
    m = D(2028, 2, 29)
    assert dates.regular_coupon(m, 1) == D(2027, 8, 31)
    assert dates.regular_coupon(m, 2) == D(2027, 2, 28)
    assert dates.regular_coupon(D(2027, 8, 31), 1) == D(2027, 2, 28)
    assert dates.regular_coupon(D(2030, 6, 30), 1) == D(2029, 12, 31)
    assert dates.regular_coupon(D(2031, 10, 15), 1) == D(2031, 4, 15)


def test_accrued_actual_actual():
    sec = note(4.25, D(2027, 11, 15))
    settle = D(2026, 10, 15)  # 153 of the 184 days from 15 May to 15 November
    assert dates.accrued_interest(sec, settle) == pytest.approx(2.125 * 153 / 184, abs=1e-12)


def test_month_end_accrued_uses_the_real_period():
    sec = note(3.0, D(2028, 2, 29))
    settle = D(2027, 9, 30)  # 30 of the 182 days from 31 August 2027 to 29 February 2028
    assert dates.coupon_period(sec, settle) == (D(2027, 8, 31), D(2028, 2, 29))
    assert dates.accrued_interest(sec, settle) == pytest.approx(1.5 * 30 / 182, abs=1e-12)


def test_coupon_on_settlement_goes_to_the_seller():
    sec = note(3.875, D(2030, 10, 15))
    settle = D(2026, 10, 15)
    assert dates.accrued_interest(sec, settle) == 0.0
    flows = dates.cash_flows(sec, settle)
    assert flows[0] == (D(2027, 4, 15), 3.875 / 2)
    assert flows[-1] == (D(2030, 10, 15), 100 + 3.875 / 2)
    assert len(flows) == 8


def test_dirty_drops_by_the_coupon_across_the_coupon_date():
    sec = note(3.875, D(2030, 10, 15))
    before = dates.dirty_price(sec, D(2026, 10, 14), 100.0)
    on = dates.dirty_price(sec, D(2026, 10, 15), 100.0)
    assert before - on == pytest.approx(3.875 / 2 * 182 / 183, abs=1e-12)


def test_bill_is_one_payment_of_100():
    bill = Security(cusip="B", kind="bill", coupon=0.0, maturity=D(2027, 4, 29))
    settle = D(2026, 11, 2)
    assert dates.accrued_interest(bill, settle) == 0.0
    assert dates.cash_flows(bill, settle) == [(D(2027, 4, 29), 100.0)]


def test_short_first_coupon():
    # dated 1 November, first coupon 15 April: a short first period inside 15 Oct to 15 Apr
    sec = note(4.0, D(2031, 10, 15), dated_date=D(2026, 11, 1), first_coupon_date=D(2027, 4, 15), first_period="Short")
    settle = D(2026, 12, 1)
    period = (D(2027, 4, 15) - D(2026, 10, 15)).days
    assert dates.cash_flows(sec, settle)[0] == (D(2027, 4, 15), pytest.approx(2.0 * 165 / period))
    assert dates.accrued_interest(sec, settle) == pytest.approx(2.0 * 30 / period)


def test_long_first_coupon_matches_the_treasury_rule():
    # 912827YG1: a 5-year 2-month note dated 1 December 1989, first coupon 15 August 1990
    sec = note(7.75, D(1995, 2, 15), dated_date=D(1989, 12, 1), first_coupon_date=D(1990, 8, 15), first_period="Long")
    stub_days = (D(1990, 2, 15) - D(1989, 8, 15)).days
    stub = (D(1990, 2, 15) - D(1989, 12, 1)).days / stub_days
    flows = dates.cash_flows(sec, D(1990, 1, 2))
    assert flows[0] == (D(1990, 8, 15), pytest.approx(3.875 * (1 + stub)))
    assert dates.accrued_interest(sec, D(1990, 1, 2)) == pytest.approx(3.875 * 32 / stub_days)
    later = D(1990, 4, 2)  # past the regular start, still before the first payment
    assert dates.accrued_interest(sec, later) == pytest.approx(3.875 * stub + 3.875 * 46 / 181)


def test_relative_bond_runs_in_fia():
    from bond_analytics import FIA

    sec = note(4.25, D(2027, 11, 15))
    settle = D(2026, 10, 15)
    bond = dates.to_relative_bond(sec, settle, clean=100.5)
    assert bond.get_price() == pytest.approx(100.5 + 2.125 * 153 / 184)
    assert bond.get_dates() == [31 / 365.25, (31 + 181) / 365.25, (31 + 181 + 184) / 365.25]
    assert bond.get_cashflows() == [2.125, 2.125, 102.125]
    flat = FIA.create_flat_curve(4.0, compounding=2)
    y = bond.YTM(bond.price(flat), compounding=2)
    assert y == pytest.approx(4.0, abs=1e-6)
