"""Redemption: one conditional UPDATE, so a voucher pays out once however many tills try.

The race tests are the reason this file exists. Each thread gets its own test client, so each
request opens its own SQLite connection exactly as it would under a threaded server, and a
barrier releases them together. Exactly one 200 must come back per voucher, and exactly one
``redeemed`` event must reach the outbox. A read-then-write version of the endpoint -- look the
voucher up outside the write lock, check its status in Python, then update by code alone --
passes every other test in this file and fails both of these.
"""

import threading
from collections import Counter
from datetime import timedelta

import pytest

from conftest import START, body, issue, redeem, rows


def issued(client, tokens, key="k", amount_cents=2500):
    return issue(client, tokens["issuer"], amount_cents=amount_cents, key=key).get_json()["code"]


def test_redeem_marks_the_voucher_and_writes_one_outbox_event(app, client, tokens, clock):
    code = issued(client, tokens, amount_cents=4200)
    clock.advance(days=3, hours=2)
    response = redeem(client, tokens["merchant"], code)
    assert response.status_code == 200
    assert response.get_json() == {"code": code, "status": "redeemed", "amount_cents": 4200,
                                   "redeemed_at": "2026-01-18T11:00:00Z"}
    [voucher] = rows(app, "select * from vouchers")
    assert voucher["status"] == "redeemed" and voucher["redeemed_by"] == "merchant"
    events = rows(app, "select * from outbox order by outbox_id")
    assert [e["event_type"] for e in events] == ["issued", "redeemed"]
    assert events[1]["occurred_at"] == "2026-01-18T11:00:00Z"
    assert events[1]["amount_cents"] == 4200
    assert events[1]["expires_at"] == events[0]["expires_at"]


def test_a_second_redemption_is_409_and_writes_nothing(app, client, tokens):
    code = issued(client, tokens)
    assert redeem(client, tokens["merchant"], code).status_code == 200
    again = redeem(client, tokens["merchant"], code)
    assert again.status_code == 409 and body(again) == {"error": "voucher cannot be redeemed"}
    assert len(rows(app, "select * from outbox where event_type = 'redeemed'")) == 1


@pytest.mark.parametrize("code", ["VCH-ffffffff", "VCH-FFFFFFFF", "VCH-123", "anything",
                                  "VCH-0000000g"])
def test_an_unknown_or_malformed_code_is_404(client, tokens, code):
    issued(client, tokens)
    response = redeem(client, tokens["merchant"], code)
    assert response.status_code == 404 and body(response) == {"error": "not found"}


def test_a_voucher_is_unredeemable_from_its_expiry_second_before_any_sweep(app, client, tokens,
                                                                          clock):
    early, late = issued(client, tokens, key="a"), issued(client, tokens, key="b")
    clock.set(START + timedelta(days=30) - timedelta(seconds=1))
    assert redeem(client, tokens["merchant"], early).status_code == 200
    clock.set(START + timedelta(days=30))
    assert redeem(client, tokens["merchant"], late).status_code == 409
    assert rows(app, "select status from vouchers where code = ?", late) == \
        [{"status": "unredeemed"}]  # lapsed, not yet swept


def race(app, code, token, tills):
    barrier = threading.Barrier(tills)
    statuses = []

    def till():
        client = app.test_client()
        barrier.wait()
        statuses.append(redeem(client, token, code).status_code)

    threads = [threading.Thread(target=till) for _ in range(tills)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return Counter(statuses)


def test_two_threads_redeeming_one_voucher_exactly_one_gets_200(app, client, tokens):
    codes = [issued(client, tokens, key=f"k{i}") for i in range(25)]
    for code in codes:
        assert race(app, code, tokens["merchant"], tills=2) == Counter({200: 1, 409: 1})
    redeemed = rows(app, "select voucher_code, count(*) as n from outbox"
                         " where event_type = 'redeemed' group by voucher_code")
    assert sorted(r["voucher_code"] for r in redeemed) == sorted(codes)
    assert {r["n"] for r in redeemed} == {1}


def test_eight_tills_racing_for_one_voucher_pay_out_once(app, client, tokens):
    code = issued(client, tokens)
    assert race(app, code, tokens["merchant"], tills=8) == Counter({200: 1, 409: 7})
    assert len(rows(app, "select * from outbox where event_type = 'redeemed'")) == 1
