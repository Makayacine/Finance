"""The simulator: it draws what its parameters say, and it reproduces itself to the byte.

The notebook judges the breakage estimator against ``true_breakage_rate``, so the first thing to
check is that the plan really is drawn from the stated parameters -- otherwise a good estimator
would look biased and a biased one might look good. ``plan`` makes no API calls, so the full
two-year plan is checked here in well under a second. The end-to-end runs are a few hundred
vouchers each, with a sweep outage moved inside their window.
"""

import json
import math
import sqlite3
import statistics
from collections import Counter
from datetime import date

import pytest

from conftest import sim

SMALL = sim.Parameters(scale=0.05, days=70, sweep_outage=("2024-12-09", "2024-12-15"))


def test_the_true_breakage_rate_is_the_stated_formula():
    p = sim.Parameters()
    assert sim.true_breakage(p) == pytest.approx(0.18 + 0.82 * math.exp(-(30 / 9) ** 0.8))
    assert round(sim.true_breakage(p), 4) == 0.2397
    curve = [sim.true_redeemed_within(p, d) for d in range(31)]
    assert curve[0] == 0 and all(a < b for a, b in zip(curve, curve[1:]))
    assert curve[30] + sim.true_breakage(p) == pytest.approx(1.0)


def test_the_plan_is_drawn_from_its_parameters():
    p = sim.Parameters()
    vouchers = sim.plan(p)
    assert 47_000 < len(vouchers) < 52_000
    never = sum(not v.redeems for v in vouchers) / len(vouchers)
    assert never == pytest.approx(p.never_redeem_share, abs=0.006)
    delays = [v.delay_seconds / 86400 for v in vouchers if v.redeems]
    weibull_median = p.delay_scale_days * math.log(2) ** (1 / p.delay_shape)
    assert statistics.median(delays) == pytest.approx(weibull_median, rel=0.03)
    lapsing = sum(d >= p.term_days for d in delays) / len(delays)
    assert lapsing == pytest.approx(math.exp(-(30 / 9) ** 0.8), abs=0.005)
    assert all(7 <= v.issued_at.hour < 19 for v in vouchers)

    per_day = Counter(v.issued_at.date() for v in vouchers)
    def mean_of(days):
        return statistics.mean(per_day[d] for d in days)
    all_days = [date.fromordinal(sim.from_text(p.start).date().toordinal() + i)
                for i in range(p.days)]
    month_end = {d for d in all_days
                 if d in sim.month_end_days(d.year, d.month, p.month_end_business_days)}
    plain_weekdays = [d for d in all_days if d.weekday() < 5 and d not in month_end]
    assert mean_of(plain_weekdays) == pytest.approx(72, rel=0.03)
    assert mean_of([d for d in all_days if d.weekday() == 5]) / mean_of(plain_weekdays) == \
        pytest.approx(0.55, abs=0.05)
    assert mean_of([d for d in all_days if d.weekday() == 6]) / mean_of(plain_weekdays) == \
        pytest.approx(0.35, abs=0.05)
    assert mean_of(month_end) / mean_of(plain_weekdays) == pytest.approx(2.0, abs=0.12)


def test_month_end_is_the_last_three_business_days():
    assert sim.month_end_days(2026, 2, 3) == {date(2026, 2, 25), date(2026, 2, 26),
                                              date(2026, 2, 27)}
    assert sim.month_end_days(2026, 5, 3) == {date(2026, 5, 27), date(2026, 5, 28),
                                              date(2026, 5, 29)}  # the 30th and 31st are a weekend


def test_a_run_reproduces_itself_to_the_byte(tmp_path):
    sim.simulate(tmp_path / "a.db", SMALL, tmp_path / "a.json")
    sim.simulate(tmp_path / "b.db", SMALL, tmp_path / "b.json")
    assert (tmp_path / "a.json").read_bytes() == (tmp_path / "b.json").read_bytes()


def test_a_run_adds_up_and_the_service_agrees(tmp_path):
    truth = sim.simulate(tmp_path / "v.db", SMALL, tmp_path / "truth.json")
    r = truth["realised"]
    assert r["issued"] > 150 and r["replays"] >= 1 and r["refused_lapsed"] >= 1
    assert r["outbox_events"] == r["issued"] + r["redeemed"] + r["expired"]
    assert r["issued"] == r["redeemed"] + r["expired"] + r["open_at_as_of"]
    assert json.loads((tmp_path / "truth.json").read_text())["realised"] == r

    conn = sqlite3.connect(tmp_path / "v.db")
    vouchers = conn.execute("select count(*) from vouchers").fetchone()[0]
    keys = conn.execute("select count(*) from idempotency_keys").fetchone()[0]
    # The outage week's expiries are swept on the 16th but dated at their own expiry.
    late = conn.execute(
        "select count(*), min(o.occurred_at), max(o.occurred_at), min(v.swept_at)"
        " from outbox o join vouchers v on v.code = o.voucher_code"
        " where o.event_type = 'expired' and o.occurred_at >= '2024-12-09T01:00:00Z'"
        " and o.occurred_at < '2024-12-16T01:00:00Z'").fetchone()
    conn.close()
    assert vouchers == keys == r["issued"]
    assert late[0] > 0 and late[2] < "2024-12-16" and late[3] == "2024-12-16T01:00:00Z"


def test_a_scaled_run_leaves_the_committed_truth_alone(tmp_path, monkeypatch, capsys):
    committed = tmp_path / "committed" / "simulation-truth.json"
    committed.parent.mkdir()
    committed.write_text("the committed truth\n")
    monkeypatch.setattr(sim, "TRUTH", committed)
    assert sim.default_truth_path(sim.Parameters(), tmp_path / "v.db") == committed
    assert sim.main(["--db", str(tmp_path / "run" / "v.db"), "--scale", "0.02", "--days", "5"]) == 0
    assert committed.read_text() == "the committed truth\n"
    written = json.loads((tmp_path / "run" / "simulation-truth.json").read_text())
    assert (written["parameters"]["scale"], written["parameters"]["days"]) == (0.02, 5)
    assert "vouchers" in capsys.readouterr().out
