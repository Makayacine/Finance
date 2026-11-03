"""The expiry sweep: overdue and unredeemed only, dated at expiry, and harmless to repeat."""

from datetime import timedelta

from conftest import START, issue, redeem, rows
from voucher_service import db
from voucher_service.__main__ import main
from voucher_service.sweep import sweep


def run_sweep(app, moment):
    conn = db.connect(app.config["DB_PATH"])
    try:
        return sweep(conn, moment)
    finally:
        conn.close()


def test_the_sweep_expires_only_overdue_unredeemed_vouchers(app, client, tokens, clock):
    old = [issue(client, tokens["issuer"], key=f"old{i}").get_json()["code"] for i in range(3)]
    clock.advance(days=1)
    assert redeem(client, tokens["merchant"], old[0]).status_code == 200
    clock.advance(days=9)
    young = issue(client, tokens["issuer"], key="young").get_json()["code"]

    assert run_sweep(app, START + timedelta(days=30)) == 2
    status = {r["code"]: r["status"] for r in rows(app, "select code, status from vouchers")}
    assert status == {old[0]: "redeemed", old[1]: "expired", old[2]: "expired",
                      young: "unredeemed"}
    expired = rows(app, "select voucher_code from outbox where event_type = 'expired'")
    assert sorted(r["voucher_code"] for r in expired) == sorted(old[1:])


def test_the_expired_event_is_dated_at_expiry_not_at_the_sweep(app, client, tokens):
    code = issue(client, tokens["issuer"], amount_cents=777).get_json()["code"]
    late = START + timedelta(days=45)
    run_sweep(app, late)
    [voucher] = rows(app, "select * from vouchers")
    [event] = rows(app, "select * from outbox where event_type = 'expired'")
    assert event["voucher_code"] == code and event["amount_cents"] == 777
    assert event["occurred_at"] == event["expires_at"] == "2026-02-14T09:00:00Z"
    assert event["issued_at"] == "2026-01-15T09:00:00Z"
    assert voucher["swept_at"] == "2026-03-01T09:00:00Z"


def test_a_second_sweep_expires_nothing(app, client, tokens):
    issue(client, tokens["issuer"])
    assert run_sweep(app, START + timedelta(days=31)) == 1
    assert run_sweep(app, START + timedelta(days=32)) == 0
    assert len(rows(app, "select * from outbox where event_type = 'expired'")) == 1


def test_the_sweep_and_the_redemption_guard_agree_on_the_boundary(app, client, tokens, clock):
    code = issue(client, tokens["issuer"]).get_json()["code"]
    assert run_sweep(app, START + timedelta(days=30, seconds=-1)) == 0
    clock.set(START + timedelta(days=30))
    assert redeem(client, tokens["merchant"], code).status_code == 409
    assert run_sweep(app, START + timedelta(days=30)) == 1
    assert redeem(client, tokens["merchant"], code).status_code == 409


def test_an_expired_voucher_cannot_be_redeemed(app, client, tokens, clock):
    code = issue(client, tokens["issuer"]).get_json()["code"]
    run_sweep(app, START + timedelta(days=30))
    clock.set(START + timedelta(days=1))  # even with a clock that has gone backwards
    assert redeem(client, tokens["merchant"], code).status_code == 409


def test_the_sweep_command(app, client, tokens, capsys):
    issue(client, tokens["issuer"])
    assert main(["sweep", "--db", app.config["DB_PATH"], "--now", "2026-02-14T09:00:00Z"]) == 0
    assert "expired 1 voucher(s)" in capsys.readouterr().out
    assert rows(app, "select status from vouchers") == [{"status": "expired"}]
