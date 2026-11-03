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

import hashlib
import json
import re
import sqlite3
import uuid
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


VIEWS = ("voucher_event", "voucher_state", "daily_liability", "redemption_life_table",
         "breakage_by_issue_month", "reconciliation")


def views_code():
    """views.sql, lower case, comments stripped."""
    text = (PROJECT / "warehouse" / "views.sql").read_text().lower()
    return "\n".join(line.split("--")[0] for line in text.splitlines())


def balanced(code, opening):
    """The text inside the parentheses that open at ``code[opening]``."""
    depth = 0
    for position in range(opening, len(code)):
        depth += {"(": 1, ")": -1}.get(code[position], 0)
        if depth == 0:
            return code[opening + 1:position]
    raise AssertionError("unbalanced parentheses")


def windows(code):
    """(function name, frame) for every ``f(...) over (...)`` in the code."""
    found = []
    for match in re.finditer(r"\)\s*over \(", code):
        depth, position = 0, match.start()
        while True:  # walk back to the parenthesis that the call's ``)`` closes
            depth += {")": 1, "(": -1}.get(code[position], 0)
            if depth == 0:
                break
            position -= 1
        name = re.search(r"(\w+)\s*$", code[:position]).group(1)
        found.append((name, balanced(code, match.end() - 1)))
    return found


def test_the_views_keep_to_sql_redshift_also_runs():
    code = views_code()
    for duckdb_only in ("qualify", "generate_series", "datediff", "date_diff", "::",
                        "strftime", "epoch(", "list_", "struct"):
        assert duckdb_only not in code, duckdb_only

    # Every window aggregate with an ORDER BY has an explicit frame: Redshift requires one.
    found = windows(code)
    aggregates = [(name, frame) for name, frame in found
                  if name in ("sum", "count", "avg", "min", "max")]
    assert len(aggregates) >= 10 and {"lag", "lead", "row_number"} <= {n for n, _ in found}
    for name, frame in aggregates:
        if "order by" in frame:
            assert "rows between" in frame, (name, frame)

    # A cast to floating point is spelled double precision, the one spelling both engines read.
    assert code.count("as double precision)") >= 5
    assert not re.search(r"as (double|float\w*|real)\b(?! precision)", code)

    # Ages are seconds from extract(epoch from ...), never a timestamp subtraction or a date part.
    assert re.findall(r"extract\(epoch from \w\.(as_of|issued_at)\)", code) == \
        ["as_of", "issued_at"] * 2
    for other in ("age(", "date_part", "date_trunc", "julian", "epoch_ms", "interval",
                  "timestampdiff"):
        assert other not in code, other
    assert not re.search(r"(as_of|issued_at|occurred_at|expires_at)\s*-\s*\w", code)


def test_the_views_read_the_same_when_integers_divide_as_integers(chain, tmp_path):
    """Redshift divides an integer by an integer as an integer; DuckDB does not, unless told to.
    A second warehouse is built from the same bucket with ``integer_division`` on -- before the
    views are created, because DuckDB fixes a view's division when it creates it -- and every
    view must give the same figures as the first, which holds only if every division of two
    integers is cast to double first. Sums of doubles may differ in the last digit: DuckDB adds
    them in parallel, in no fixed order."""
    con, service_db, _ = chain
    redshift_like = warehouse.connect(tmp_path / "redshift-like.duckdb")
    redshift_like.execute("set integer_division = true")
    redshift_like.execute("create temp view probe as select cast(7 as bigint) / 2 as q")
    assert redshift_like.execute("select q from probe").fetchone()[0] == 3
    warehouse.create(redshift_like)
    warehouse.copy_new(redshift_like, service_db.parent / "bucket")
    as_of = con.execute("select as_of from warehouse_as_of").fetchone()[0]
    warehouse.set_as_of(redshift_like, as_of)
    redshift_like.executemany("insert into service_control_total values (?, ?, ?)",
                              con.execute("select * from service_control_total").fetchall())
    for view in VIEWS:
        query = f"select * from {view} order by all"
        as_redshift, as_duckdb = fetch(redshift_like, query), fetch(con, query)
        assert len(as_redshift) == len(as_duckdb) > 0, view
        for row, expected in zip(as_redshift, as_duckdb):
            assert row.keys() == expected.keys()
            for name, value in row.items():
                want = expected[name]
                if isinstance(want, float):
                    assert value == pytest.approx(want, rel=1e-12), (view, name)
                else:
                    assert value == want, (view, name)
    redshift_like.close()


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


def test_an_open_voucher_is_projected_from_its_exact_age(tmp_path):
    """Three vouchers from 1 January -- one redeems on day 0, two lapse on day 30 -- and a fourth
    issued twelve hours before as_of, still open halfway through its day 0.

    Day 0: 400 at risk, 100 redeemed, 100 censored at half exposure, so the hazard is
    100 / 350 and S(0) = 5/7; the two lapses make the final expired share 5/7. Voucher 4 has
    come through half of day 0 without redeeming, so its survival is 1 - 0.5 * 2/7 = 6/7 and its
    chance of lapsing (5/7) / (6/7) = 5/6. Reading survival at the start of the day instead would
    give 5/7 and understate the cohort's breakage."""
    con = warehouse.connect(tmp_path / "w.duckdb")
    warehouse.create(con)
    start = datetime(2026, 1, 1, 9)
    as_of = start + timedelta(days=30, hours=1)
    late_issue = as_of - timedelta(hours=12)
    events = []

    def event(voucher, kind, issued_at, at):
        days = (at - issued_at).days
        events.append((f"00000000-0000-4000-8000-{len(events):012d}", f"VCH-0000000{voucher}",
                       kind, 100, at, issued_at, issued_at + timedelta(days=30),
                       date(2026, 1, 1), days, days if kind == "redeemed" else None))

    for voucher in (1, 2, 3):
        event(voucher, "issued", start, start)
    event(4, "issued", late_issue, late_issue)
    event(1, "redeemed", start, start + timedelta(hours=2))
    event(2, "expired", start, start + timedelta(days=30))
    event(3, "expired", start, start + timedelta(days=30))
    con.executemany("insert into voucher_events_raw values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    events)
    warehouse.set_as_of(con, as_of)

    [day0] = fetch(con, "select * from redemption_life_table where scope = 'pooled' and day = 0")
    assert day0["survival"] == pytest.approx(5 / 7)
    [cohort] = fetch(con, "select * from breakage_by_issue_month")
    assert cohort["open_cents"] == 100
    assert cohort["breakage_rate"] == pytest.approx((200 + 100 * 5 / 6) / 400)


def test_nothing_dated_after_as_of_is_visible(chain, tmp_path):
    """The backtest rewinds as_of to each month end and trusts the views to hide every later
    event. Rewound to midday on 20 December, the warehouse must hold exactly the service's
    events up to that second, and its open vouchers must be the service's open vouchers then."""
    _, service_db, _ = chain
    con = warehouse.connect(tmp_path / "w.duckdb")
    warehouse.create(con)
    warehouse.copy_new(con, service_db.parent / "bucket")
    mid = "2024-12-20T12:00:00Z"
    warehouse.set_as_of(con, mid)

    service = sqlite3.connect(service_db)
    events = service.execute("select count(*) from outbox where occurred_at <= ?",
                             (mid,)).fetchone()[0]
    later = service.execute("select count(*) from outbox where occurred_at > ?",
                            (mid,)).fetchone()[0]
    open_count, open_cents = service.execute(
        "select count(*), sum(amount_cents) from vouchers where issued_at <= ?1"
        " and (redeemed_at is null or redeemed_at > ?1) and expires_at > ?1", (mid,)).fetchone()
    service.close()
    assert later > 100 and open_count > 30

    assert con.execute("select count(*) from voucher_event").fetchone()[0] == events
    assert con.execute("select max(occurred_at) from voucher_event").fetchone()[0] <= \
        datetime(2024, 12, 20, 12)
    state = fetch(con, "select count(*) as vouchers, sum(amount_cents) as cents"
                       " from voucher_state where status = 'unredeemed'")[0]
    assert state == {"vouchers": open_count, "cents": open_cents}
    last = fetch(con, "select * from daily_liability order by day desc limit 1")[0]
    assert last["day"] == date(2024, 12, 20) and last["outstanding_cents"] == open_cents
    assert con.execute("select max(day) from redemption_life_table where scope = 'cohort'"
                       " and issue_month = date '2024-12-01'").fetchone()[0] <= 19


def hand_built(tmp_path, as_of, rows):
    """A warehouse loaded with ``rows`` -- (voucher, kind, issued_at, term_days, at) -- as the
    transform would have derived them, and reporting as of ``as_of``."""
    con = warehouse.connect(tmp_path / "w.duckdb")
    warehouse.create(con)
    events = []
    for n, (voucher, kind, issued_at, term_days, at) in enumerate(rows):
        days = int((at - issued_at).total_seconds() // 86400)
        # Event ids in no particular order, as uuid4s are: nothing may hang on them.
        event_id = str(uuid.UUID(bytes=hashlib.md5(str(n).encode()).digest()))
        events.append((event_id, f"VCH-{voucher:08x}", kind, 100,
                       at, issued_at, issued_at + timedelta(days=term_days),
                       issued_at.date().replace(day=1), days,
                       days if kind == "redeemed" else None))
    con.executemany("insert into voucher_events_raw values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    events)
    warehouse.set_as_of(con, as_of)
    return con


def test_two_exits_and_an_orphan_exit_are_counted_and_the_earlier_exit_wins(tmp_path):
    """None of this can come through the service, whose guards allow one exit per voucher; the
    views must still decide each case one way, and the reconciliation must say it happened.
    Voucher 1 is redeemed and later expired: the redemption stands. Vouchers 2 to 21 are
    redeemed and expired in the same second, half of them arriving one way round and half the
    other: the expiry stands every time, as the redemption guard has it at the expiry second.
    Voucher 22 has an exit and no issue. Voucher 23 is open, and its issue arrives twice."""
    t0 = datetime(2026, 1, 1, 9)
    expiry = t0 + timedelta(days=30)
    rows = [(1, "issued", t0, 30, t0), (1, "redeemed", t0, 30, t0 + timedelta(days=1)),
            (1, "expired", t0, 30, expiry)]
    for voucher in range(2, 22):
        exits = ["redeemed", "expired"] if voucher % 2 else ["expired", "redeemed"]
        rows += [(voucher, "issued", t0, 30, t0)] + [(voucher, kind, t0, 30, expiry)
                                                     for kind in exits]
    rows += [(22, "redeemed", t0, 30, t0 + timedelta(days=2)),
             (23, "issued", t0 + timedelta(days=10), 30, t0 + timedelta(days=10))]
    con = hand_built(tmp_path, t0 + timedelta(days=31), rows)
    con.execute("insert into voucher_events_raw select * from voucher_events_raw"
                " where voucher_code = 'VCH-00000017'")  # voucher 23
    con.execute("insert into service_control_total values (?, 1, 100)", [expiry])
    state = {int(r["voucher_code"][4:], 16): (r["status"], r["exited_at"])
             for r in fetch(con, "select * from voucher_state")}
    assert state == {1: ("redeemed", t0 + timedelta(days=1)),
                     **{voucher: ("expired", expiry) for voucher in range(2, 22)},
                     23: ("unredeemed", None)}
    check = warehouse.reconciliation(con)
    assert (check["orphan_exits"], check["vouchers_with_two_exits"]) == (1, 21)
    assert (check["duplicates_dropped"], check["difference_cents"]) == (1, 0)


def test_an_open_voucher_is_projected_from_its_exact_expired_share_too(tmp_path):
    """The other half of the exact-age interpolation: an expiry inside an open voucher's last
    day. The warehouse takes each voucher's term from its events, so the terms here differ.
    Voucher 1 (a 2-day term) lapses on day 2; voucher 2 redeems on day 5; voucher 3 lapses on
    day 10; voucher 4 is open, 2.5 days old, so it is censored on day 2 at half exposure.

    Day 2: 400 at risk less half of 100 censored, 100 expires: hazard 2/7, S(2) = 5/7, expired
    share 2/7. Day 5: 200 at risk, 100 redeems: S = 5/14. Day 10: the last 100 lapses, so the
    final expired share is 2/7 + 5/14 = 9/14. Halfway through day 2 voucher 4 has S = 6/7 and
    expired share 1/7, so its chance of lapsing is (9/14 - 1/7) / (6/7) = 7/12. Read at the
    start of the day the expired share would be 0, and at the end 2/7: 3/4 or 5/12."""
    t0 = datetime(2026, 1, 1, 9)
    as_of = t0 + timedelta(days=10, hours=1)
    late = as_of - timedelta(hours=60)
    con = hand_built(tmp_path, as_of, [
        (1, "issued", t0, 2, t0), (1, "expired", t0, 2, t0 + timedelta(days=2)),
        (2, "issued", t0, 10, t0), (2, "redeemed", t0, 10, t0 + timedelta(days=5)),
        (3, "issued", t0, 10, t0), (3, "expired", t0, 10, t0 + timedelta(days=10)),
        (4, "issued", late, 10, late),
    ])
    table = [(r["day"], r["censored_cents"], r["survival"], r["expired_share"]) for r in
             fetch(con, "select * from redemption_life_table where scope = 'pooled' order by day")]
    assert table == [(2, 100, pytest.approx(5 / 7), pytest.approx(2 / 7)),
                     (5, 0, pytest.approx(5 / 14), pytest.approx(2 / 7)),
                     (10, 0, 0.0, pytest.approx(9 / 14))]
    [cohort] = fetch(con, "select * from breakage_by_issue_month")
    assert (cohort["issued_cents"], cohort["expired_cents"], cohort["open_cents"]) == \
        (400, 200, 100)
    assert cohort["breakage_rate"] == pytest.approx((200 + 100 * 7 / 12) / 400)


def test_the_control_total_is_never_stamped_earlier_than_the_service_it_read(chain, tmp_path):
    """The service can only be read as it is now. Asked to report as of a moment before the
    service's latest change, the warehouse refuses and keeps the as_of and total it had;
    without an as_of, it reports as of that change."""
    _, service_db, truth = chain
    bucket, path = service_db.parent / "bucket", tmp_path / "w.duckdb"
    con, _ = warehouse.build(service_db, bucket, path)
    close = datetime.strptime(truth["as_of"], "%Y-%m-%dT%H:%M:%SZ")
    assert con.execute("select as_of from warehouse_as_of").fetchone()[0] == close
    before = warehouse.reconciliation(con)
    assert before["taken_at"] == close and before["difference_cents"] == 0
    con.close()

    with pytest.raises(warehouse.ServiceMovedOn):
        warehouse.build(service_db, bucket, path, "2024-12-20T00:00:00Z")
    assert warehouse.main(["--service-db", str(service_db), "--bucket-dir", str(bucket),
                           "--warehouse", str(path), "--as-of", "2024-12-20T00:00:00Z"]) == 2
    con = warehouse.connect(path)
    assert con.execute("select as_of from warehouse_as_of").fetchall() == [(close,)]
    assert warehouse.reconciliation(con) == before
    con.close()
