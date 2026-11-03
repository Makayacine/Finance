"""Login, tokens and roles: 401 before 403, and nothing in an error that an attacker can use.

The order matters as much as the codes. A request without a valid token is 401 whatever else is
wrong with it, so an anonymous caller cannot probe which voucher codes exist or what a valid body
looks like; a valid token with the wrong role is 403 before the body or the code is looked at.
"""

import time

import jwt
import pytest

from conftest import PASSWORDS, SECRET, USERS, body, issue, login, redeem, rows
from voucher_service.app import create_app
from voucher_service.config import SeedUser


def test_login_returns_a_jwt_carrying_the_role(client):
    response = client.post("/login", json={"username": "finance", "password": "finance-pass"})
    assert response.status_code == 200
    payload = response.get_json()
    claims = jwt.decode(payload["access_token"], SECRET, algorithms=["HS256"])
    assert claims["sub"] == "finance" and claims["role"] == "finance"
    assert claims["exp"] - claims["iat"] == payload["expires_in"] == 3600
    assert payload["token_type"] == "Bearer"


def test_passwords_are_stored_as_argon2id_hashes_and_never_in_plaintext(app):
    stored = {row["username"]: row for row in rows(app, "select * from users")}
    assert {name: row["role"] for name, row in stored.items()} == \
        {"issuer": "issuer", "merchant": "merchant", "finance": "finance"}
    for name, row in stored.items():
        assert row["password_hash"].startswith("$argon2id$")
        assert PASSWORDS[name] not in row["password_hash"]


def test_a_wrong_password_and_an_unknown_user_get_the_same_401(client):
    wrong = client.post("/login", json={"username": "issuer", "password": "nope"})
    unknown = client.post("/login", json={"username": "nobody", "password": "nope"})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.get_data() == unknown.get_data() == b'{"error":"invalid credentials"}\n'


@pytest.mark.parametrize("payload", [
    {"username": "issuer"},
    {"username": "issuer", "password": "issuer-pass", "role": "finance"},
    {"username": "issuer", "password": 12345},
    {"username": "", "password": "issuer-pass"},
    ["issuer", "issuer-pass"],
])
def test_a_malformed_login_body_is_400(client, payload):
    response = client.post("/login", json=payload)
    assert response.status_code == 400 and body(response) == {"error": "invalid request"}


def test_json_nested_past_the_parsers_limit_is_400_not_500(app, client, tokens):
    deep = "[" * 15_000  # inside the 16 KB limit, far past Python's recursion limit
    response = client.post("/login", data=deep, content_type="application/json")
    assert response.status_code == 400 and body(response) == {"error": "invalid request"}
    response = client.post("/vouchers", data=deep, content_type="application/json",
                           headers={"Authorization": f"Bearer {tokens['issuer']}",
                                    "Idempotency-Key": "deep"})
    assert response.status_code == 400 and body(response) == {"error": "invalid request"}
    assert rows(app, "select count(*) as n from vouchers") == [{"n": 0}]


def test_no_token_is_401_on_both_writes(client):
    for response in (client.post("/vouchers", json={}), redeem(client, "", "VCH-00000000")):
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == "Bearer"
        assert body(response) == {"error": "authentication required"}


def forged(**overrides):
    now = int(time.time())
    claims = {"sub": "issuer", "role": "issuer", "iat": now, "exp": now + 600, **overrides}
    return claims


@pytest.mark.parametrize("header", [
    "Bearer not-a-jwt",
    "Bearer " + jwt.encode(forged(), "some-other-key-that-is-also-32-bytes-long", "HS256"),
    "Bearer " + jwt.encode(forged(iat=1, exp=2), SECRET, "HS256"),
    "Bearer " + jwt.encode(forged(), None, "none"),
    "Bearer " + jwt.encode(forged(role="admin"), SECRET, "HS256"),
    "Bearer " + jwt.encode({"sub": "issuer", "role": "issuer"}, SECRET, "HS256"),  # no exp
    "Basic aXNzdWVyOmlzc3Vlci1wYXNz",
], ids=["garbage", "other-key", "expired", "alg-none", "unknown-role", "no-expiry", "basic"])
def test_a_bad_token_is_401(client, header):
    response = client.post("/vouchers", json={"amount_cents": 100, "recipient_id": "c1"},
                           headers={"Authorization": header, "Idempotency-Key": "k"})
    assert response.status_code == 401


def test_the_wrong_role_is_403(client, tokens):
    assert issue(client, tokens["merchant"]).status_code == 403
    assert issue(client, tokens["finance"]).status_code == 403
    code = issue(client, tokens["issuer"]).get_json()["code"]
    assert redeem(client, tokens["issuer"], code).status_code == 403
    assert redeem(client, tokens["finance"], code).status_code == 403
    assert body(redeem(client, tokens["finance"], code)) == {"error": "forbidden"}


def test_authentication_and_role_come_before_the_body_and_the_code(client, tokens):
    assert client.post("/vouchers", data="not json").status_code == 401
    assert redeem(client, "", "VCH-ffffffff").status_code == 401
    assert issue(client, tokens["merchant"], amount_cents=-1, key=None).status_code == 403
    assert redeem(client, tokens["issuer"], "not-a-code").status_code == 403


def test_a_new_password_in_config_replaces_the_stored_hash(app, client):
    changed = [SeedUser(u.username, u.role, u.password + "-rotated") for u in USERS]
    rotated = create_app(app.config["DB_PATH"], jwt_secret=SECRET, users=changed).test_client()
    assert rotated.post("/login", json={"username": "issuer",
                                        "password": "issuer-pass"}).status_code == 401
    login(rotated, "issuer", "issuer-pass-rotated")


def test_errors_are_generic_json_everywhere(client):
    assert client.get("/nowhere").status_code == 404
    assert body(client.get("/nowhere")) == {"error": "not found"}
    assert client.get("/vouchers").status_code == 405
    assert body(client.get("/vouchers")) == {"error": "method not allowed"}
    oversized = client.post("/login", data="x" * 20_000, content_type="application/json")
    assert oversized.status_code == 413


def test_the_bearer_scheme_is_case_insensitive(client, tokens):
    lowercase = client.post("/vouchers", json={"amount_cents": 1, "recipient_id": "c"},
                            headers={"Authorization": f"bearer {tokens['issuer']}",
                                     "Idempotency-Key": "k2"})
    assert lowercase.status_code == 201
