"""The South African parsers: the Treasury's auction sheets and debt schedule, the OECD and IMF files.

The sheets are built here as grids in each layout the Treasury has used, with figures taken from
its files, because its licence keeps the spreadsheets themselves out of the repository. The
schedule lines are as pypdf reads them from the schedules of 31 March 2017 and 30 September 2026.
"""

import datetime as dt

import pytest

from bond_analytics import nt_auctions as na
from bond_analytics import nt_schedule as ns
from bond_analytics import sa_reference

D = dt.date
DT = dt.datetime

SCHEDULE_2026 = """SCHEDULE OF DOMESTIC GOVERNMENT BONDS AS AT 30 SEPTEMBER 2026 Page 1 of 2
R187* 10.50% 22-May-98 78.230%   333 333 333 21/06 & 21/12 21-Dec-26 78 459 919 187
RN2027*** 8.300% 06-Jul-22 100.000%  22 595 000 000 11/10, 11/01, 11/04 & 11/07 11-Jul-27 71 075 000 000
R210** 2.60% 27-Sep-07 96.289%   100 000 000 31/03 & 30/09 31-Mar-28 53 310 805 634
R2030 8.00% 04-Oct-13 95.261%   300 000 000 28/02 & 31/08 31-Jan-30 297 978 110 031
RS2031 10.64% 22-Nov-23 100.000%  8 866 000 000 31/01 & 31/07 31-Mar-31 8 866 000 000
R213 7.00% 28-May-10 80.922%   300 000 000 28/02 & 31/08 28-Feb-31 218 177 161 244
R2048* 8.75% 29-Jun-12 99.075%   166 666 667 28/02 & 31/08 28-Feb-48 156 145 724 939
R2053 11.625% 11-Apr-23 99.780%  1 300 000 000 31/03 & 30/09 31-Mar-53 118 877 382 543
R001 4.50% 01-Dec-86 - - 01/01 & 01/07 INDEFINITE   10 410
Retail Bonds VARIABLE 24-May-04 - - VARIOUS VARIOUS 30 353 460 925
"""

SCHEDULE_2017 = """SCHEDULE OF DOMESTIC GOVERNMENT BONDS AS AT 31 MARCH 2017
R186* 10.50% 22-May-98 78.230%   333 333 333 21/06 & 21/12 21-Dec-25 59 669 545 130
R186* 10.50% 22-May-98 78.230%   333 333 333 21/06 & 21/12 21-Dec-26 59 669 545 130
R186* 10.50% 22-May-98 78.230%   333 333 334 21/06 & 21/12 21-Dec-27 59 669 545 130
R2048* 8.75% 29-Jun-12 99.075%   166 666 667 28/02 & 31/08 28-Feb-47 45 062 283 632
R2048* 8.75% 29-Jun-12 99.075%   166 666 667 28/02 & 31/08 28-Feb-48 45 062 283 632
R2048* 8.75% 29-Jun-12 99.075%   166 666 667 28/02 & 31/08 28-Feb-49 45 062 283 632
"""


def schedule():
    return ns.merge([ns.bonds(ns.parse_lines(SCHEDULE_2017), D(2017, 3, 31)),
                     ns.bonds(ns.parse_lines(SCHEDULE_2026), D(2026, 9, 30))])


# --- the debt schedule ------------------------------------------------------------------------


def test_only_fixed_rate_bonds_are_kept():
    codes = set(ns.bonds(ns.parse_lines(SCHEDULE_2026), D(2026, 9, 30)))
    assert codes == {"R187", "R2030", "R213", "R2048", "R2053"}  # no FRN, ILB, retail or perpetual


def test_a_line_reads_into_its_fields():
    b = ns.bonds(ns.parse_lines(SCHEDULE_2026), D(2026, 9, 30))["R2053"]
    assert (b["coupon"], b["issue_date"], b["issue_price"]) == (11.625, D(2023, 4, 11), 99.78)
    assert b["redemptions"] == [D(2053, 3, 31)] and b["nominal_rand"] == 118_877_382_543
    assert (b["coupon_months"], b["coupon_days"], b["quality_flags"]) == ("3,9", "31,30", "")


def test_r2030s_listed_interest_dates_disagree_with_its_redemption_and_the_redemption_wins():
    b = ns.bonds(ns.parse_lines(SCHEDULE_2026), D(2026, 9, 30))["R2030"]
    assert b["interest_dates_listed"] == "28/02 & 31/08"
    assert (b["coupon_months"], b["coupon_days"]) == ("1,7", "31,31")
    assert b["quality_flags"] == "interest_dates_disagree_with_redemption"


def test_a_three_legged_bond_is_priced_on_its_middle_leg_and_its_legs_add_up():
    b = schedule()["R186"]
    assert b["legs"] == 3 and b["pricing_maturity"] == D(2026, 12, 21)
    assert b["nominal_rand"] == pytest.approx(3 * 59_669_545_130)


def test_the_latest_schedule_listing_a_bond_wins():
    b = schedule()["R2048"]  # three legs in 2017; split by 2026, R2048 is then the middle leg alone
    assert b["schedule_date"] == D(2026, 9, 30) and b["redemptions"] == [D(2048, 2, 28)]
    assert b["pricing_maturity"] == D(2048, 2, 28)
    assert schedule()["R186"]["schedule_date"] == D(2017, 3, 31)


def test_the_as_at_date_survives_the_treasurys_misspelling():
    assert ns.schedule_date("Schedule of domestic government bonds as at 31 Janaury 2023.pdf") == D(2023, 1, 31)
    assert ns.schedule_date(SCHEDULE_2026) == D(2026, 9, 30)


# --- the auction sheets -----------------------------------------------------------------------

LONG = [
    ["Bond Auctioned", "Bond coupon(%)", "Redemption date", "Auction date", "Settlement date", "Offer amount (R'm)",
     "Allocated amount (R'm)", "Clearing yield", "Total number of bids received (R'm)", "Bid-to-cover ratio",
     "Market rates", "Spread (Issue yield-Market rate)", "Non comps "],
    [213, 7.0, DT(2031, 2, 28), DT(2010, 5, 25), DT(2010, 5, 28), 300, 300, 9.05, 945, 3.15, None, None, 0],
    [0, 8.75, DT(2048, 2, 28), DT(2012, 6, 26), DT(2012, 6, 29), 500, 500, 8.83, 1630, 3.26, None, None, 151],
]


def wide(label_col=0, unit=1.0, first_date="3 April 2012"):
    """A month sheet: labels down column ``label_col``, two auctions of two and one bonds."""
    pad = [None] * label_col
    rows = [
        pad + ["Fixed-rate government bond auction results, April 2012"],
        pad + ["Auction date", first_date, None, DT(2012, 4, 10)],
        pad + ["Settlement date", DT(2012, 4, 6), None, DT(2012, 4, 13)],
        pad + [None],
        pad + ["Bonds auctioned", "R204", "R'213", 2023],
        pad + ["Bond coupon; redemption", "8.00%, 2018", "(7%;2031)", "7.75%:2023"],
        pad + ["Total amount allocated (R)", 700_000_000, 700_000_000, 500_000_000],
        pad + ["Total number of bids received", 27, 32, 25],
        pad + ["Total amount of bids received (R)", 1_835_000_000, 1_075_000_000, 1_110_000_000],
        pad + ["Clearing yield", 7.57 * unit, 8.76 * unit, 6.76 * unit],
        pad + ["Best bid", 7.51 * unit, 8.64 * unit, 6.5 * unit],
        pad + ["Worst bid", 7.685 * unit, 8.81 * unit, 6.9 * unit],
        pad + ["Bid to cover ratio (times)", 2.62, 1.54, 2.22],
    ]
    return rows


def test_the_long_table_reads_rand_millions_as_rand_and_has_no_bid_range():
    rows = na.parse_grid(LONG, "fixed-rate-bonds-2010-13.xls", "Fixed-rate bonds")
    r213, r0 = rows
    assert (r213["bond_code"], r213["bond_label"], r213["redemption_year_quoted"]) == ("R213", "213", 2031)
    assert (r213["allocated_rand"], r213["bids_received_rand"]) == (300e6, 945e6)
    assert (r213["clearing_yield"], r213["best_bid"], r213["yield_unit"]) == (9.05, None, "percent")
    assert r0["bond_code"] is None and r0["bond_label"] == "0"


@pytest.mark.parametrize("label_col", [0, 1])
def test_a_month_sheet_reads_with_its_labels_in_either_column(label_col):
    rows = na.parse_grid(wide(label_col), "fixed-rate-bonds-2012-13.xls", "April 2012")
    assert [(r["auction_date"], r["settle_date"], r["bond_code"]) for r in rows] == [
        (D(2012, 4, 3), D(2012, 4, 6), "R204"), (D(2012, 4, 3), D(2012, 4, 6), "R213"),
        (D(2012, 4, 10), D(2012, 4, 13), "R2023")]
    assert [r["bond_label"] for r in rows] == ["R204", "R'213", "2023"]
    assert [(r["coupon_quoted"], r["redemption_year_quoted"]) for r in rows] == [(8.0, 2018), (7.0, 2031), (7.75, 2023)]
    assert [r["number_of_bids"] for r in rows] == [27, 32, 25]


def test_yields_stated_as_fractions_become_percent_and_say_so():
    rows = na.parse_grid(wide(unit=0.01), "fixed-rate-bonds-2012-13.xls", "April 2012")
    assert [r["clearing_yield"] for r in rows] == [7.57, 8.76, 6.76]
    assert [r["best_bid"] for r in rows] == [7.51, 8.64, 6.5]
    assert {r["yield_unit"] for r in rows} == {"fraction"}
    assert {r["yield_unit"] for r in na.parse_grid(wide(), "x", "y")} == {"percent"}


def test_a_second_empty_block_on_a_sheet_adds_nothing():
    grid = wide() + [[None]] * 3 + [["Auction date"], ["Settlement date"], ["Bonds auctioned"], ["Clearing yield"]]
    assert len(na.parse_grid(grid, "x", "February 2016")) == 3


@pytest.mark.parametrize("text, terms", [
    ("(7%;2031)", (7.0, 2031)), ("8.00%, 2018", (8.0, 2018)), ("8%; 2030", (8.0, 2030)),
    ("(8.875%: 2035)", (8.875, 2035)), ("(8.000%:2030)", (8.0, 2030)), ("", (None, None)),
])
def test_the_coupon_and_redemption_text_in_every_spelling(text, terms):
    assert na.parse_terms(text) == terms


def test_a_files_name_gives_the_fiscal_years_it_covers():
    assert na.fiscal_range("fixed-rate-bonds-2010-13.xls") == (D(2010, 4, 1), D(2013, 3, 31))
    assert na.fiscal_range("Fixed-rate bond auctions - 2019-20.xls") == (D(2019, 4, 1), D(2020, 3, 31))


# --- the checks -------------------------------------------------------------------------------


def auction(day, code, y, best, worst, settle=None, source="fixed-rate-bonds-2025-26.xlsx", sheet="April", flags=""):
    return {"auction_date": day, "settle_date": settle or day + dt.timedelta(days=3), "bond_code": code,
            "bond_label": code, "coupon_quoted": None, "redemption_year_quoted": None, "clearing_yield": y,
            "best_bid": best, "worst_bid": worst, "source_file": source, "sheet": sheet, "quality_flags": flags}


def flags(r):
    return set(filter(None, r["quality_flags"].split(",")))


def test_r2053s_1_85_on_22_april_2025_is_outside_its_bids_and_out_of_the_curve():
    (r,) = na.flag([auction(D(2025, 4, 22), "R2053", 1.85, 11.76, 12.00)], schedule())
    assert flags(r) == {"clearing_outside_bids"} and not na.usable(r)
    (ok,) = na.flag([auction(D(2025, 4, 29), "R2053", 11.88, 11.76, 12.00)], schedule())
    assert flags(ok) == set() and na.usable(ok)


def test_no_bid_range_is_said_but_the_yield_still_counts():
    (r,) = na.flag([auction(D(2010, 5, 25), "R213", 9.05, None, None)], schedule())
    assert flags(r) == {"no_bid_range"} and na.usable(r)


def test_the_sheets_coupon_and_year_are_checked_against_the_schedule_without_blocking():
    r = auction(D(2025, 5, 13), "R2048", 11.2, 11.1, 11.3)
    r.update(coupon_quoted=8.5, redemption_year_quoted=2037)
    (r,) = na.flag([r], schedule())
    assert flags(r) == {"coupon_differs_from_schedule", "maturity_year_differs_from_schedule"} and na.usable(r)


def test_a_bond_settling_before_its_issue_marks_its_whole_auction():
    rows = na.flag([auction(D(2023, 4, 4), "R2053", 11.65, 11.5, 11.8, settle=D(2023, 4, 7)),
                    auction(D(2023, 4, 4), "R2030", 10.58, 10.5, 10.7, settle=D(2023, 4, 7)),
                    auction(D(2023, 4, 11), "R2030", 10.6, 10.5, 10.7)], schedule())
    assert flags(rows[0]) == {"before_issue_date", "misdated_auction"}
    assert flags(rows[1]) == {"misdated_auction"} and flags(rows[2]) == set()


def test_a_settlement_before_the_auction_is_doubtful():
    (r,) = na.flag([auction(D(2014, 7, 8), "R2048", 9.03, 9.0, 9.1, settle=D(2014, 6, 11))], schedule())
    assert flags(r) == {"settlement_date_doubtful"} and not na.usable(r)


def test_two_files_that_disagree_on_a_date_both_stay_out():
    longt = dict(source="fixed-rate-bonds-2010-13.xls", sheet="Fixed-rate bonds")
    year = dict(source="fixed-rate-bonds-2012-13.xls", sheet="August 2012")
    rows = na.flag([auction(D(2012, 8, 21), "R2048", 8.40, None, None, **longt),
                    auction(D(2012, 8, 21), "R2048", 9.50, 9.40, 9.75, **year),
                    auction(D(2012, 8, 28), "R2048", 8.53, None, None, **longt),
                    auction(D(2012, 8, 28), "R2048", 8.53, 8.45, 8.66, **year)], schedule())
    assert ["files_disagree" in flags(r) for r in rows] == [True, True, False, False]
    kept = na.deduplicate(rows)
    assert [(r["clearing_yield"], r["source_file"][-11:]) for r in kept] == [(9.5, "2012-13.xls"), (8.53, "2012-13.xls")]
    assert [na.usable(r) for r in kept] == [False, True]


def test_a_misdated_row_never_replaces_the_real_one():
    real = auction(D(2013, 3, 19), "R213", 8.32, None, None, source="fixed-rate-bonds-2010-13.xls")
    wrong = auction(D(2013, 3, 19), "R213", 9.15, 9.04, 9.35, source="fixed-rate-bonds-2012-13.xls",
                    flags="misdated_auction")
    kept = na.deduplicate([real, wrong])
    assert sorted(r["clearing_yield"] for r in kept) == [8.32, 9.15]
    assert [r["clearing_yield"] for r in kept if na.usable(r)] == [8.32]


def test_a_spike_that_comes_straight_back_is_flagged_but_a_move_across_the_curve_is_not():
    rows = [auction(D(2012, 8, 14), "R2048", 8.31, 8.2, 8.5), auction(D(2012, 8, 21), "R2048", 9.50, 9.4, 9.75),
            auction(D(2012, 8, 28), "R2048", 8.53, 8.4, 8.7),
            auction(D(2021, 3, 23), "R2044", 10.94, 10.8, 11.1), auction(D(2021, 3, 23), "R2048", 10.81, 10.7, 10.9),
            auction(D(2021, 3, 30), "R2044", 11.545, 11.4, 11.9), auction(D(2021, 3, 30), "R2048", 11.50, 11.4, 11.8),
            auction(D(2021, 4, 6), "R2044", 10.87, 10.8, 11.0), auction(D(2021, 4, 6), "R2048", 10.84, 10.7, 10.9)]
    out = na.flag_neighbours(rows)
    hit = [(r["auction_date"], r["bond_code"]) for r in out if "off_neighbouring_auctions" in flags(r)]
    assert hit == [(D(2012, 8, 21), "R2048")]
    assert na.flag_neighbours(out) == out  # set afresh, never stacked


def test_a_row_dated_outside_its_files_year_is_flagged_on_parse():
    import io

    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "January"
    for row in wide(first_date=DT(2018, 1, 8)):
        ws.append(row)
    ws["D2"], ws["B3"], ws["D3"] = DT(2018, 1, 15), DT(2018, 1, 11), DT(2018, 1, 18)
    buf = io.BytesIO()
    wb.save(buf)
    rows = na.parse_file(buf.getvalue(), "fixed-rate-bonds-2018-19.xlsx")
    assert len(rows) == 3 and {r["quality_flags"] for r in rows} == {"date_outside_fiscal_year"}
    assert na.parse_file(buf.getvalue(), "fixed-rate-bonds-2017-18.xlsx")[0]["quality_flags"] == ""


# --- the OECD and IMF series --------------------------------------------------------------------

OECD = (
    "STRUCTURE,STRUCTURE_ID,STRUCTURE_NAME,ACTION,REF_AREA,Reference area,FREQ,Frequency of observation,MEASURE,"
    "Measure,UNIT_MEASURE,Unit of measure,TIME_PERIOD,Time period,OBS_VALUE,Observation value\n"
    "DATAFLOW,OECD.SDD.STES:DSD_STES@DF_FINMARK(4.0),Financial market,I,ZAF,South Africa,M,Monthly,IRLT,"
    "Long-term interest rates,PA,Percent per annum,2026-09,,8.947143,\n"
    "DATAFLOW,OECD.SDD.STES:DSD_STES@DF_FINMARK(4.0),Financial market,I,ZAF,South Africa,M,Monthly,IRLT,"
    "Long-term interest rates,PA,Percent per annum,2026-08,,8.749,\n"
).encode()
IMF = b"observation_date,INTGSBZAM193N\n2026-08-01,8.75\n2026-09-01,8.97\n2026-10-01,\n"


def test_the_oecd_rows_are_monthly_and_carry_their_citation():
    rows = sa_reference.parse_oecd(OECD, D(2026, 11, 3))
    assert [(r["date"], r["percent"]) for r in rows] == [(D(2026, 8, 1), 8.749), (D(2026, 9, 1), 8.947143)]
    assert rows[0]["source"].startswith("OECD (2026), Financial market") and "accessed on 2026-11-03" in rows[0]["source"]


def test_the_imf_rows_leave_out_a_missing_month_and_name_the_imf():
    rows = sa_reference.parse_imf(IMF)
    assert [(r["date"], r["percent"]) for r in rows] == [(D(2026, 8, 1), 8.75), (D(2026, 9, 1), 8.97)]
    assert rows[0]["source"].startswith("Source: International Monetary Fund, International Financial Statistics")
    with pytest.raises(ValueError):
        sa_reference.parse_imf(b"DATE,OTHER\n2026-01-01,1\n")
