"""The three parsers: FedInvest's page, the GSW file and the Treasury's auction records."""

import datetime as dt

import pytest

from bond_analytics import gsw
from bond_analytics.fedinvest import FedInvestFormatError, page_date, parse_prices, security_from_row
from bond_analytics.securities import run_label, run_ranks, static_by_cusip

D = dt.date

HEADER = "<tr><th>CUSIP</th><th>SECURITY TYPE</th><th>RATE</th><th>MATURITY DATE</th><th>CALL DATE</th><th>BUY</th><th>SELL</th><th>END OF DAY</th></tr>"


def page(day_text, rows, header=HEADER, table=True):
    body = "".join(
        "<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows
    )
    tbl = f'<table class="data1">{header}{body}</table>' if table else ""
    return f"<html><body><h2>Prices For: {day_text}</h2><table><tr><td>layout</td></tr></table>{tbl}</body></html>"


ROWS = [
    ["912797VP9", "MARKET BASED BILL", "0.000%", "11/03/2026", "", "0.000000", "99.957333", "99.989389"],
    ["91282CMW8", "MARKET BASED NOTE", "3.875%", "04/15/2028", "", "100.1", "100.0", "100.031250"],
    ["912810QA9", "MARKET BASED BOND", "3.500%", "02/15/2039", "", "0", "0", "95.5"],
    ["912828ZJ2", "TIPS", "0.125%", "04/15/2027", "", "0", "0", "99.1"],
    ["91282CNY3", "MARKET BASED FRN", "0.150%", "10/31/2028", "", "0", "0", "100.02"],
]


def test_parse_page():
    rows = parse_prices(page("October 30, 2026", ROWS), D(2026, 10, 30))
    assert len(rows) == 5
    assert rows[1]["cusip"] == "91282CMW8" and rows[1]["end_of_day"] == "100.031250"
    assert [security_from_row(r).kind for r in rows] == ["bill", "note", "bond", "tips", "frn"]
    sec = security_from_row(rows[1])
    assert sec.coupon == 3.875 and sec.maturity == D(2028, 4, 15)


def test_a_holiday_page_has_no_table_and_no_rows():
    assert parse_prices(page("October 12, 2026", [], table=False), D(2026, 10, 12)) == []


def test_a_page_for_another_date_is_refused():
    with pytest.raises(FedInvestFormatError, match="2026-10-29"):
        parse_prices(page("October 30, 2026", ROWS), D(2026, 10, 29))
    assert page_date(page("June 16, 2008", [])) == D(2008, 6, 16)


def test_a_changed_header_or_short_row_is_refused():
    with pytest.raises(FedInvestFormatError, match="header"):
        parse_prices(page("October 30, 2026", ROWS, header=HEADER.replace("END OF DAY", "EOD")), D(2026, 10, 30))
    with pytest.raises(FedInvestFormatError, match="cells"):
        parse_prices(page("October 30, 2026", [ROWS[0][:7]]), D(2026, 10, 30))
    with pytest.raises(FedInvestFormatError, match="unknown security type"):
        security_from_row(dict(ROWS and parse_prices(page("October 30, 2026", ROWS), D(2026, 10, 30))[0], security_type="STRIPS"))


GSW_TEXT = """"Note: This is not an official Federal Reserve statistical release."

Series,Compounding Convention,Mnemonic(s)
Parameters,N/A,BETA0 to TAU2

Date,BETA0,BETA1,BETA2,BETA3,SVENY01,SVENY02,TAU1,TAU2
1979-12-31,10.42,2.25,-1.0,0,11.5,11.2,2.19,-999.99
2026-10-23,5.48106339177429e-13,4.01039120164686,4.51159915720968,16.1535165152315,4.4,4.6416,1.92317991734059,15.8327487462404
2026-10-26,NA,NA,NA,NA,NA,NA,NA,NA
"""


def test_gsw_rows():
    rows = list(gsw.read_rows(GSW_TEXT))
    assert [r["date"] for r in rows] == [D(1979, 12, 31), D(2026, 10, 23), D(2026, 10, 26)]
    assert rows[0]["tau2"] is None and rows[0]["beta3"] == 0.0  # Nelson-Siegel years
    assert gsw.params_of(rows[1])[3] == pytest.approx(16.1535165152315)
    assert rows[1]["sveny02"] == pytest.approx(4.6416)
    assert rows[2]["beta0"] is None


def test_gsw_without_header_is_refused():
    with pytest.raises(ValueError, match="Date,"):
        list(gsw.read_rows("a,b\n1,2\n"))


def auction(cusip, term, issue, reopening="No", kind="Note", tips="No", frn="No", dated=None):
    return {
        "cusip": cusip, "security_type": kind, "original_security_term": term, "issue_date": issue,
        "dated_date": dated or issue, "first_int_payment_date": "null", "first_int_period": "Normal",
        "reopening": reopening, "inflation_index_security": tips, "floating_rate": frn,
    }


def test_static_keeps_the_original_auction():
    recs = [
        auction("A", "10-Year", "2026-09-15", reopening="Yes", dated="2026-08-15"),
        auction("A", "10-Year", "2026-08-15"),
        auction("A", "10-Year", "2026-10-15", reopening="Yes", dated="2026-08-15"),
    ]
    st = static_by_cusip(recs)["A"]
    assert st["issue_date"] == D(2026, 8, 15) and st["original_term"] == "10-Year"


def test_run_ranks():
    recs = [
        auction("N1", "10-Year", "2026-05-15"),
        auction("N2", "10-Year", "2026-08-15"),
        auction("N3", "10-Year", "2026-11-16"),  # not issued yet on the date asked
        auction("N0", "10-Year", "2026-02-15"),
        auction("T1", "10-Year", "2026-09-15", tips="Yes"),
        auction("F1", "2-Year", "2026-10-31", frn="Yes"),
        auction("O1", "29-Year 9-Month", "1990-01-15", kind="Bond"),
    ]
    ranks = run_ranks(static_by_cusip(recs), D(2026, 11, 2))
    assert ranks == {"N2": 0, "N1": 1, "N0": 2}
    assert [run_label(ranks.get(c)) for c in ("N2", "N1", "N0", "O1")] == [
        "on-the-run", "first off-the-run", "off-the-run", "off-the-run"]
