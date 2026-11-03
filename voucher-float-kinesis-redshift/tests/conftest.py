"""Shared fixtures: a fresh service database per test, a settable clock, and the three logins.

Every test runs the real app over a real SQLite file in a temporary folder, through Flask's test
client: the same routes, transactions and SQL the server runs. Where a test needs a failure, it
makes the database fail -- a trigger that aborts an insert -- rather than patching the code that
would see it. The one stand-in anywhere is in ``test_relay.py``: a client that answers
``put_records`` the way a throttled Kinesis stream does.
"""

import importlib.util
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from voucher_service.app import create_app  # noqa: E402
from voucher_service.config import SeedUser  # noqa: E402
from voucher_service.timeutil import SettableClock  # noqa: E402

SECRET = "test-signing-key-that-is-at-least-32-bytes-long"
USERS = [SeedUser("issuer", "issuer", "issuer-pass"),
         SeedUser("merchant", "merchant", "merchant-pass"),
         SeedUser("finance", "finance", "finance-pass")]
PASSWORDS = {user.username: user.password for user in USERS}
START = datetime(2026, 1, 15, 9, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def clock():
    return SettableClock(START)


@pytest.fixture
def app(tmp_path, clock):
    service = create_app(tmp_path / "vouchers.db", clock=clock, jwt_secret=SECRET, users=USERS)
    service.config["TESTING"] = True
    return service


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def tokens(client):
    return {name: login(client, name) for name in PASSWORDS}


def login(client, username, password=None):
    response = client.post("/login", json={"username": username,
                                           "password": password or PASSWORDS[username]})
    assert response.status_code == 200, response.get_json()
    return response.get_json()["access_token"]


def bearer(token, **extra):
    return {"Authorization": f"Bearer {token}", **extra}


def issue(client, token, amount_cents=2500, recipient_id="cust-0001", key="key-1"):
    headers = bearer(token, **({"Idempotency-Key": key} if key is not None else {}))
    return client.post("/vouchers", json={"amount_cents": amount_cents,
                                          "recipient_id": recipient_id}, headers=headers)


def issue_raw(client, token, raw, key="key-1"):
    """Send ``raw`` bytes as the body, for bodies ``json=`` cannot express."""
    return client.post("/vouchers", data=raw, content_type="application/json",
                       headers=bearer(token, **{"Idempotency-Key": key}))


def redeem(client, token, code):
    return client.post(f"/vouchers/{code}/redeem", headers=bearer(token))


def rows(app, sql, *params):
    conn = sqlite3.connect(app.config["DB_PATH"])
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql, params)]
    finally:
        conn.close()


def counts(app):
    return {table: rows(app, f"select count(*) as n from {table}")[0]["n"]
            for table in ("vouchers", "outbox", "idempotency_keys")}


def body(response):
    return json.loads(response.get_data(as_text=True))


def load_module(relative_path, name):
    """Import a file by path. The lab names its entrypoints with hyphens -- ``outbox-relay.py`` --
    and a hyphen is not a Python identifier, so the cost is paid here, once."""
    spec = importlib.util.spec_from_file_location(name, PROJECT / relative_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module
