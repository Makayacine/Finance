"""The warehouse: dedupe, state from occurred_at, a late sweep that changes nothing, and a
reconciliation that catches a lost event.

Most tests run the whole local chain on a small simulation -- the REST service, the relay into a
folder stream, the transform through the Firehose stand-in, the COPY into DuckDB and the views in
``warehouse/views.sql`` -- because the properties worth having are properties of the chain: a
duplicate the relay sent must vanish, a late expiry must land on the day it belongs to, and the
warehouse's liability must equal the service's own total to the cent.

The life table is also worked by hand, on four vouchers, so its arithmetic is pinned down by
something other than itself.
"""

import json
import re
import sqlite3
from datetime import date, datetime, timedelta

import pytest

from conftest import (CATCH_UP, ON_TIME, PROJECT, SMALL, fetch, firehose, run_chain, sim,
                      transform, warehouse)


def ddl_columns(path):
    tables = {}
    text = path.read_text()
    for name, body in re.findall(r"create table if not exists (\w+) \((.*?)\n\)", text, re.S):
        tables[name] = re.findall(r"^\s+(\w+)\s+(\w+(?:\(\d+\))?)", body, re.M)
    return tables


def test_the_local_tables_are_the_redshift_tables():
    local = ddl_columns(PROJECT / "warehouse" / "landing-tables.sql")
    redshift = ddl_columns(PROJECT / "redshift" / "redshift-create-tables.sql")
    assert local == redshift
    assert set(local) == {"voucher_events_raw", "warehouse_as_of", "service_control_total"}


def test_the_transform_emits_exactly_the_landing_columns():
    payload = json.dumps({"event_id": "3f0c6c1e-8a4b-4d43-9a37-5b1f8f0e2a11",
                          "voucher_code": "VCH-5e1a9c07", "event_type": "issued",
                          "amount_cents": 100, "occurred_at": "2026-10-05T09:14:22Z",
                          "issued_at": "2026-10-05T09:14:22Z",
                          "expires_at": "2026-11-04T09:14:22Z"}).encode()
    keys = list(json.loads(transform.transform(payload)))
    assert keys == [name for name, _ in warehouse.landing_columns()]


def test_the_views_keep_to_sql_redshift_also_runs():
    text = (PROJECT / "warehouse" / "views.sql").read_text().lower()
    code = "\n".join(line.split("--")[0] for line in text.splitlines())
    for duckdb_only in ("qualify", "generate_series", "datediff", "date_diff", "::",
                        "strftime", "epoch(", "list_", "struct"):
        assert duckdb_only not in code, duckdb_only
    for window in re.findall(r"sum\([^;]*?over \((.*?)\)", code, re.S):
        if "order by" in window:
            assert "rows between" in window  # Redshift wants a frame with ORDER BY


def test_the_service_and_the_warehouse_reconcile_to_the_cent(chain):
    con, service_db, truth = chain
    check = warehouse.reconciliation(con)
    assert check["difference_cents"] == 0
    assert check["warehouse_cents"] == truth["realised"]["open_cents_at_as_of"] > 0
    assert check["warehouse_vouchers"] == check["service_vouchers"] == \
        truth["realised"]["open_at_as_of"]
    assert (check["duplicates_dropped"], check["orphan_exits"],
            check["vouchers_with_two_exits"]) == (0, 0, 0)
    last = fetch(con, "select * from daily_liability order by day desc limit 1")[0]
    assert last["outstanding_cents"] == check["warehouse_cents"]


def test_the_warehouse_counts_what_the_simulator_did(chain):
    con, _, truth = chain
    r = truth["realised"]
    totals = fetch(con, "select sum(issued_vouchers) as issued, sum(redeemed_vouchers) as redeemed,"
                        " sum(expired_vouchers) as expired, sum(issued_cents) as cents"
                        " from breakage_by_issue_month")[0]
    assert totals == {"issued": r["issued"], "redeemed": r["redeemed"],
                      "expired": r["expired"], "cents": r["issued_cents"]}


def test_copies_are_dropped_on_event_id(tmp_path):
    con, service_db, _ = run_chain(tmp_path)
    before = fetch(con, "select * from daily_liability order by day")
    events = con.execute("select count(*) from voucher_event").fetchone()[0]
    # Firehose delivers the whole stream a second time: a replay after a lost checkpoint.
    (tmp_path / "checkpoint.json").unlink()
    firehose.deliver(tmp_path / "stream", tmp_path / "bucket", tmp_path / "checkpoint.json")
    warehouse.copy_new(con, tmp_path / "bucket")
    assert con.execute("select count(*) from voucher_events_raw").fetchone()[0] == 2 * events
    assert con.execute("select count(*) from voucher_event").fetchone()[0] == events
    assert fetch(con, "select * from daily_liability order by day") == before
    check = warehouse.reconciliation(con)
    assert check["difference_cents"] == 0 and check["duplicates_dropped"] == events


def test_a_lost_redemption_shows_up_in_the_reconciliation(tmp_path):
    con, _, _ = run_chain(tmp_path)
    lost = fetch(con, "select event_id, amount_cents from voucher_events_raw"
                      " where event_type = 'redeemed' order by occurred_at desc limit 1")[0]
    con.execute("delete from voucher_events_raw where event_id = ?", [lost["event_id"]])
    check = warehouse.reconciliation(con)
    assert check["difference_cents"] == lost["amount_cents"]
    assert check["warehouse_vouchers"] == check["service_vouchers"] + 1


def test_a_sweep_a_week_late_gives_the_same_daily_liability(tmp_path):
    on_time, _, _ = run_chain(tmp_path / "on-time", ON_TIME)
    late, late_db, _ = run_chain(tmp_path / "late", SMALL)
    series = "select * from daily_liability order by day"

    # The late run really did reorder the stream: the catch-up sweep's expiries were written
    # after redemptions dated up to a week later than them.
    service = sqlite3.connect(late_db)
    catch_up_ids = [row[0] for row in service.execute(
        "select o.outbox_id from outbox o join vouchers v on v.code = o.voucher_code"
        " where o.event_type = 'expired' and v.swept_at = ? order by o.outbox_id", (CATCH_UP,))]
    first_late = catch_up_ids[0]
    oldest_late = service.execute("select min(occurred_at) from outbox where outbox_id >= ?"
                                  " and event_type = 'expired'", (first_late,)).fetchone()[0]
    newest_before = service.execute("select max(occurred_at) from outbox where outbox_id < ?",
                                    (first_late,)).fetchone()[0]
    published_later = [row[0] for row in service.execute(
        "select event_id from outbox where outbox_id >= ?", (first_late,))]
    service.close()
    assert len(catch_up_ids) > 3
    gap = sim.from_text(newest_before) - sim.from_text(oldest_late)
    assert gap > timedelta(days=6)

    # Before the catch-up arrives, the outage week's liability is overstated...
    late.execute("create table held_back as select * from voucher_events_raw"
                 " where event_id in (select unnest(?))", [published_later])
    late.execute("delete from voucher_events_raw where event_id in (select event_id from held_back)")
    just_before = datetime(2024, 12, 16, 0, 59, 59)
    for con in (on_time, late):
        warehouse.set_as_of(con, just_before)
    early = {r["day"]: r["outstanding_cents"] for r in fetch(on_time, series)}
    stale = {r["day"]: r["outstanding_cents"] for r in fetch(late, series)}
    outage_days = [date(2024, 12, 9) + timedelta(days=i) for i in range(7)]
    assert all(stale[d] > early[d] for d in outage_days[1:])
    assert all(stale[d] == early[d] for d in stale if d < date(2024, 12, 8))

    # ...and once it arrives, every past day is corrected: the two series are identical.
    late.execute("insert into voucher_events_raw select * from held_back")
    for con in (on_time, late):
        warehouse.set_as_of(con, "2025-01-13T00:00:00Z")
    assert fetch(late, series) == fetch(on_time, series)
    assert len(fetch(late, series)) == 70


def test_a_closed_cohort_reports_its_realised_breakage_and_an_open_one_is_projected(chain):
    con, _, _ = chain
    cohorts = fetch(con, "select * from breakage_by_issue_month order by issue_month")
    closed = [c for c in cohorts if c["open_vouchers"] == 0]
    young = [c for c in cohorts if c["open_vouchers"] > 0]
    assert closed and young
    for c in closed:
        assert c["breakage_rate"] == pytest.approx(c["realised_breakage_rate"])
        assert c["naive_breakage_rate"] == pytest.approx(c["realised_breakage_rate"])
    for c in young:
        assert c["realised_breakage_rate"] is None
        floor = c["expired_cents"] / c["issued_cents"]
        ceiling = (c["expired_cents"] + c["open_cents"]) / c["issued_cents"]
        assert floor < c["breakage_rate"] < ceiling


def test_the_life_table_worked_by_hand(tmp_path):
    """Four 1.00 vouchers in one cohort. Voucher 1 redeems on day 0 and voucher 2 on day 2,
    voucher 3 lapses on day 30, and voucher 4 -- issued 25 days after the others -- is still
    open at as_of, five days old, so it is censored on day 5 and counts half that day."""
    con = warehouse.connect(tmp_path / "w.duckdb")
    warehouse.create(con)
    start = datetime(2026, 1, 1, 9)
    late_issue = start + timedelta(days=25)
    events = []

    def event(voucher, kind, issued_at, at):
        days = (at - issued_at).days
        events.append((f"00000000-0000-4000-8000-{len(events):012d}", f"VCH-0000000{voucher}",
                       kind, 100, at, issued_at, issued_at + timedelta(days=30),
                       issued_at.date().replace(day=1), days,
                       days if kind == "redeemed" else None))

    for voucher in (1, 2, 3):
        event(voucher, "issued", start, start)
    event(4, "issued", late_issue, late_issue)
    event(1, "redeemed", start, start + timedelta(hours=5))
    event(2, "redeemed", start, start + timedelta(days=2, hours=1))
    event(3, "expired", start, start + timedelta(days=30))
    con.executemany("insert into voucher_events_raw values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    events)
    warehouse.set_as_of(con, start + timedelta(days=30, hours=1))

    table = fetch(con, "select day, at_risk_cents, redeemed_cents, expired_cents,"
                       " censored_cents, survival, redeemed_share, expired_share"
                       " from redemption_life_table where scope = 'pooled' order by day")
    expected = [  # day, at risk, redeemed, expired, censored, S, redeemed share, expired share
        (0, 400, 100, 0, 0, 0.75, 0.25, 0.0),        # 100 of 400 redeem
        (2, 300, 100, 0, 0, 0.50, 0.50, 0.0),        # 100 of 300: S = 0.75 * 2/3
        (5, 200, 0, 0, 100, 0.50, 0.50, 0.0),        # voucher 4 censored, nothing exits
        (30, 100, 0, 100, 0, 0.0, 0.50, 0.50),       # voucher 3 lapses: breakage 0.5
    ]
    got = [(r["day"], r["at_risk_cents"], r["redeemed_cents"], r["expired_cents"],
            r["censored_cents"], r["survival"], r["redeemed_share"], r["expired_share"])
           for r in table]
    assert got == [tuple(pytest.approx(v) for v in e) for e in expected]

    # Voucher 4 is alive at the start of day 5; nothing in the table redeems after day 2, so it
    # is projected to lapse: (0.5 - 0) / S(4) = 0.5 / 0.5 = 1. Breakage (100 + 100) / 400.
    [cohort] = fetch(con, "select * from breakage_by_issue_month")
    assert (cohort["issued_cents"], cohort["redeemed_cents"], cohort["expired_cents"],
            cohort["open_cents"]) == (400, 200, 100, 100)
    assert cohort["breakage_rate"] == pytest.approx(0.5)
    assert cohort["naive_breakage_rate"] == pytest.approx(100 / 300)
    assert cohort["realised_breakage_rate"] is None

    liability = [(r["day"], r["outstanding_cents"]) for r in
                 fetch(con, "select * from daily_liability order by day")]
    assert liability == [(date(2026, 1, 1), 200), (date(2026, 1, 3), 100),
                         (date(2026, 1, 26), 200), (date(2026, 1, 31), 100)]
