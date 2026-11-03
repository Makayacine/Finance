"""Issuing: integer cents, an Idempotency-Key on every request, and the outbox row beside it.

A refused request must leave no trace -- no voucher, no outbox row, and no stored key, so the
client can fix the body and retry under the same key. A failure halfway through must leave no
trace either: the test below makes the outbox insert itself fail, inside SQLite, and checks the
voucher went with it.
"""

import re
import sqlite3
import uuid
from datetime import timedelta

import pytest

from conftest import (SECRET, START, USERS, bearer, body, counts, issue, issue_raw, login, rows)
from voucher_service.app import create_app
from voucher_service.config import SeedUser

NOTHING = {"vouchers": 0, "outbox": 0, "idempotency_keys": 0}


def test_issue_returns_a_vch_code_and_a_30_day_expiry(client, tokens):
    response = issue(client, tokens["issuer"], amount_cents=2500, recipient_id="cust-0042")
    assert response.status_code == 201
    voucher = response.get_json()
    assert re.fullmatch(r"VCH-[0-9a-f]{8}", voucher["code"])
    assert voucher == {"code": voucher["code"], "status": "unredeemed", "amount_cents": 2500,
                       "recipient_id": "cust-0042", "issued_at": "2026-01-15T09:00:00Z",
                       "expires_at": "2026-02-14T09:00:00Z"}


def test_issue_writes_the_voucher_and_its_outbox_row_in_one_go(app, client, tokens):
    code = issue(client, tokens["issuer"], amount_cents=1999).get_json()["code"]
    [voucher] = rows(app, "select * from vouchers")
    [event] = rows(app, "select * from outbox")
    assert voucher["code"] == code and voucher["status"] == "unredeemed"
    assert voucher["issued_by"] == "issuer"
    assert uuid.UUID(event["event_id"]).version == 4
    assert {k: event[k] for k in ("voucher_code", "event_type", "amount_cents", "occurred_at",
                                  "issued_at", "expires_at", "published_at")} == {
        "voucher_code": code, "event_type": "issued", "amount_cents": 1999,
        "occurred_at": voucher["issued_at"], "issued_at": voucher["issued_at"],
        "expires_at": voucher["expires_at"], "published_at": None}


def test_the_same_key_and_body_returns_the_original_201_and_issues_nothing(app, client, tokens,
                                                                          clock):
    first = issue(client, tokens["issuer"], key="retry-me")
    clock.advance(minutes=5)  # a retry arrives later; the replay must not restamp anything
    second = issue(client, tokens["issuer"], key="retry-me")
    assert first.status_code == second.status_code == 201
    assert second.get_data() == first.get_data()
    assert second.headers["Idempotent-Replayed"] == "true"
    assert counts(app) == {"vouchers": 1, "outbox": 1, "idempotency_keys": 1}


def test_key_order_and_whitespace_do_not_make_a_different_body(app, client, tokens):
    first = issue_raw(client, tokens["issuer"], '{"amount_cents": 500, "recipient_id": "c-1"}')
    again = issue_raw(client, tokens["issuer"], '{ "recipient_id":"c-1",\n "amount_cents":500 }')
    assert first.status_code == again.status_code == 201
    assert again.get_data() == first.get_data()
    assert counts(app)["vouchers"] == 1


def test_the_same_key_with_a_different_body_is_422_and_issues_nothing(app, client, tokens):
    original = issue(client, tokens["issuer"], amount_cents=500, key="k")
    reused = issue(client, tokens["issuer"], amount_cents=501, key="k")
    assert original.status_code == 201 and reused.status_code == 422
    assert body(reused) == {"error": "idempotency key already used with a different request"}
    assert counts(app) == {"vouchers": 1, "outbox": 1, "idempotency_keys": 1}


def test_keys_belong_to_the_issuer_that_sent_them(tmp_path, clock):
    two = USERS + [SeedUser("issuer-2", "issuer", "second-issuer-pass")]
    app = create_app(tmp_path / "v.db", clock=clock, jwt_secret=SECRET, users=two)
    client = app.test_client()
    one = issue(client, login(client, "issuer"), key="shared")
    other = issue(client, login(client, "issuer-2", "second-issuer-pass"), key="shared",
                  amount_cents=999)
    assert one.status_code == other.status_code == 201
    assert one.get_json()["code"] != other.get_json()["code"]


@pytest.mark.parametrize("key", [None, "", "has space", "x" * 256])
def test_a_missing_or_malformed_idempotency_key_is_400(app, client, tokens, key):
    headers = bearer(tokens["issuer"], **({} if key is None else {"Idempotency-Key": key}))
    response = client.post("/vouchers", json={"amount_cents": 100, "recipient_id": "c"},
                           headers=headers)
    assert response.status_code == 400
    assert counts(app) == NOTHING


@pytest.mark.parametrize("raw", [
    '{"recipient_id": "c-1"}',                                      # amount missing
    '{"amount_cents": 100}',                                        # recipient missing
    '{"amount_cents": -100, "recipient_id": "c-1"}',                # negative
    '{"amount_cents": 0, "recipient_id": "c-1"}',                   # zero
    '{"amount_cents": 25.5, "recipient_id": "c-1"}',                # fractional
    '{"amount_cents": 2500.0, "recipient_id": "c-1"}',              # whole, but a float
    '{"amount_cents": 1e3, "recipient_id": "c-1"}',                 # exponent form is a float
    '{"amount_cents": "2500", "recipient_id": "c-1"}',              # a string
    '{"amount_cents": true, "recipient_id": "c-1"}',                # bool is an int in Python
    '{"amount_cents": null, "recipient_id": "c-1"}',
    '{"amount_cents": NaN, "recipient_id": "c-1"}',
    '{"amount_cents": 10000001, "recipient_id": "c-1"}',            # above the cap
    '{"amount_cents": 1180591620717411303424, "recipient_id": "c-1"}',  # 2**70, past int64
    '{"amount_cents": 100, "recipient_id": "c-1", "currency": "ZAR"}',  # unknown field
    '{"amount_cents": 100, "recipient_id": "jane@example.com"}',   # not an opaque id
    '{"amount_cents": 100, "recipient_id": ""}',
    '{"amount_cents": 100, "recipient_id": 42}',
    '{}',
    '[100, "c-1"]',
    'amount_cents=100',
    '',
], ids=["no-amount", "no-recipient", "negative", "zero", "fraction", "whole-float", "exponent",
        "string", "bool", "null", "nan", "over-cap", "past-int64", "unknown-field",
        "email-recipient", "empty-recipient", "numeric-recipient", "empty-object", "array",
        "form-encoded", "empty-body"])
def test_an_invalid_body_is_400_and_writes_nothing(app, client, tokens, raw):
    response = issue_raw(client, tokens["issuer"], raw)
    assert response.status_code == 400
    assert body(response) == {"error": "invalid request"}
    assert counts(app) == NOTHING


def test_a_refused_body_does_not_use_up_its_key(app, client, tokens):
    assert issue(client, tokens["issuer"], amount_cents=-5, key="fix-me").status_code == 400
    assert issue(client, tokens["issuer"], amount_cents=5, key="fix-me").status_code == 201


def test_a_code_collision_draws_a_new_code(tmp_path, clock):
    draws = iter(["VCH-0000000a", "VCH-0000000a", "VCH-0000000a", "VCH-0000000b"])
    app = create_app(tmp_path / "v.db", clock=clock, jwt_secret=SECRET, users=USERS,
                     code_factory=lambda: next(draws))
    client = app.test_client()
    token = login(client, "issuer")
    assert issue(client, token, key="a").get_json()["code"] == "VCH-0000000a"
    assert issue(client, token, key="b").get_json()["code"] == "VCH-0000000b"
    assert counts(app)["outbox"] == 2


def test_a_failed_outbox_write_takes_the_voucher_with_it(app, client, tokens):
    conn = sqlite3.connect(app.config["DB_PATH"])
    conn.execute("create trigger outbox_down before insert on outbox"
                 " begin select raise(abort, 'outbox unavailable'); end")
    conn.commit()
    failed = issue(client, tokens["issuer"], key="atomic")
    assert failed.status_code == 500 and body(failed) == {"error": "internal error"}
    assert counts(app) == NOTHING
    conn.execute("drop trigger outbox_down")
    conn.commit()
    conn.close()
    assert issue(client, tokens["issuer"], key="atomic").status_code == 201
    assert counts(app) == {"vouchers": 1, "outbox": 1, "idempotency_keys": 1}


def test_issue_and_expiry_follow_the_service_clock(client, tokens, clock):
    clock.set(START + timedelta(days=400, hours=3, microseconds=987_654))
    voucher = issue(client, tokens["issuer"]).get_json()
    assert voucher["issued_at"] == "2027-02-19T12:00:00Z"
    assert voucher["expires_at"] == "2027-03-21T12:00:00Z"
