"""The voucher service: REST for the writes that move money.

    POST /login                       username + password  ->  a JWT carrying the role
    POST /vouchers                    issuer    amount_cents, recipient_id, Idempotency-Key  ->  201
    POST /vouchers/<code>/redeem      merchant  one conditional UPDATE  ->  200, 409 or 404
    POST /graphql                     finance   the reads: voucher, liability, cohort (schema.py)

Each write changes the ``vouchers`` table and appends to ``outbox`` in one transaction, and
nothing in a request talks to the stream. ``python -m voucher_service sweep`` expires overdue
vouchers through the same outbox.

ISSUING, AND WHY IT NEEDS AN IDEMPOTENCY KEY
--------------------------------------------

Issuing creates a liability. A client whose request timed out cannot tell whether the voucher
was created, and a plain retry would create a second one. So ``POST /vouchers`` refuses to run
without an ``Idempotency-Key`` header, and the first 201 for each (issuer, key) is stored with a
hash of the request body. The same key with the same body -- key order and whitespace do not
matter -- gets that stored response back byte for byte, and no new voucher. The same key with a
different body is a client bug and gets 422, not a second voucher and not the first one's
response. Only a 201 is stored, so a request refused for a bad body can be fixed and retried
under the same key.

REDEEMING, AND WHY IT IS ONE STATEMENT
--------------------------------------

    update vouchers set status = 'redeemed', ...
     where code = ? and status = 'unredeemed' and expires_at > ?

The guard and the write are the same statement, so there is no gap between "is it still
unredeemed?" and "mark it redeemed" for a second till to slip through. Two merchants redeeming
one voucher at the same instant get one 200 and one 409, and ``tests/test_redeem.py`` races
threads to show it. When no row changes, a second read inside the same transaction tells an
unknown code (404) from a used or lapsed one (409). A voucher past its expiry is unredeemable from
that second, whether or not the sweep has marked it yet.

A code is ``VCH-`` and 32 random bits, which is short enough to read over a till but not to
ignore collisions: across 50,000 vouchers the chance of at least one repeat is about one in
four. The insert says ``on conflict do nothing`` and a collision simply draws a new code.
"""

import hashlib
import json
import secrets
from datetime import timedelta

from flask import Flask, Response, current_app, g, jsonify, request
from werkzeug.exceptions import HTTPException

from . import auth, config, db, schema
from .errors import error
from .timeutil import to_text, utc_now

CODE_ATTEMPTS = 8
ISSUE_FIELDS = {"amount_cents", "recipient_id"}


def new_code():
    return config.CODE_PREFIX + secrets.token_hex(4)


def create_app(db_path, *, clock=utc_now, jwt_secret=None, users=None, code_factory=new_code,
               warehouse=None):
    app = Flask(__name__)
    app.config.update(
        DB_PATH=str(db_path),
        JWT_SECRET=jwt_secret or config.jwt_secret() or secrets.token_urlsafe(32),
        TOKEN_TTL_SECONDS=config.TOKEN_TTL_SECONDS,
        MAX_CONTENT_LENGTH=config.MAX_REQUEST_BYTES,
        CLOCK=clock,
        CODE_FACTORY=code_factory,
        WAREHOUSE=warehouse,
    )
    db.create_schema(db_path)
    conn = db.connect(db_path)
    try:
        with db.write_transaction(conn):
            auth.seed(conn, users if users is not None else config.seed_users())
    finally:
        conn.close()

    app.teardown_appcontext(close_db)
    app.register_error_handler(HTTPException, lambda exc: error(exc.code or 500))
    app.register_error_handler(Exception, unhandled)
    app.add_url_rule("/login", view_func=login, methods=["POST"])
    app.add_url_rule("/vouchers", view_func=issue_voucher, methods=["POST"])
    app.add_url_rule("/vouchers/<code>/redeem", view_func=redeem_voucher, methods=["POST"])
    app.add_url_rule("/graphql", view_func=graphql, methods=["POST"])
    return app


def get_db():
    if "db" not in g:
        g.db = db.connect(current_app.config["DB_PATH"])
    return g.db


def close_db(_exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def unhandled(exc):
    current_app.logger.exception("unhandled error")
    return error(500)


def now():
    return current_app.config["CLOCK"]().replace(microsecond=0)


def json_body():
    """The request's JSON, or None if it is not JSON. A body nested deeper than the parser's
    recursion limit -- sixteen kilobytes of ``[`` is enough -- is not JSON either."""
    try:
        return request.get_json(silent=True)
    except RecursionError:
        return None


def login():
    body = json_body()
    if not isinstance(body, dict) or set(body) != {"username", "password"}:
        return error(400)
    username, password = body["username"], body["password"]
    if not (isinstance(username, str) and isinstance(password, str)) \
            or not 0 < len(username) <= 64 or not 0 < len(password) <= 1024:
        return error(400)
    role = auth.check_password(get_db(), username, password)
    if role is None:
        return error(401, "invalid credentials")
    ttl = current_app.config["TOKEN_TTL_SECONDS"]
    token = auth.issue_token(username, role, current_app.config["JWT_SECRET"], ttl)
    return jsonify(access_token=token, token_type="Bearer", expires_in=ttl, role=role)


def valid_issue_body(body):
    if not isinstance(body, dict) or set(body) != ISSUE_FIELDS:
        return False
    amount, recipient = body["amount_cents"], body["recipient_id"]
    # type() rather than isinstance(): JSON true is a Python bool, and a bool is an int.
    if type(amount) is not int or not 0 < amount <= config.MAX_AMOUNT_CENTS:
        return False
    return isinstance(recipient, str) and config.RECIPIENT_PATTERN.fullmatch(recipient) is not None


def request_hash(body):
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@auth.require_role("issuer")
def issue_voucher():
    key = request.headers.get("Idempotency-Key")
    body = json_body()
    if key is None or not config.IDEMPOTENCY_KEY_PATTERN.fullmatch(key) \
            or not valid_issue_body(body):
        return error(400)
    digest = request_hash(body)
    conn = get_db()
    with db.write_transaction(conn):
        prior = conn.execute(
            "select request_hash, status_code, response_body from idempotency_keys"
            " where username = ? and idempotency_key = ?", (g.username, key)).fetchone()
        if prior is not None:
            if prior["request_hash"] != digest:
                return error(422)
            replay = Response(prior["response_body"], status=prior["status_code"],
                              mimetype="application/json")
            replay.headers["Idempotent-Replayed"] = "true"
            return replay

        issued = now()
        issued_at = to_text(issued)
        expires_at = to_text(issued + timedelta(days=config.TERM_DAYS))
        code = insert_voucher(conn, body, issued_at, expires_at)
        db.append_event(conn, code, "issued", body["amount_cents"], issued_at, issued_at,
                        expires_at)
        payload = json.dumps({
            "code": code, "status": "unredeemed", "amount_cents": body["amount_cents"],
            "recipient_id": body["recipient_id"], "issued_at": issued_at,
            "expires_at": expires_at,
        }, separators=(",", ":"), sort_keys=True)
        conn.execute(
            "insert into idempotency_keys (username, idempotency_key, request_hash, status_code,"
            " response_body, created_at) values (?, ?, ?, 201, ?, ?)",
            (g.username, key, digest, payload, issued_at))
    return Response(payload, status=201, mimetype="application/json")


def insert_voucher(conn, body, issued_at, expires_at):
    for _ in range(CODE_ATTEMPTS):
        code = current_app.config["CODE_FACTORY"]()
        inserted = conn.execute(
            "insert into vouchers (code, recipient_id, amount_cents, status, issued_at,"
            " expires_at, issued_by) values (?, ?, ?, 'unredeemed', ?, ?, ?)"
            " on conflict (code) do nothing",
            (code, body["recipient_id"], body["amount_cents"], issued_at, expires_at,
             g.username)).rowcount
        if inserted:
            return code
    raise RuntimeError(f"no free voucher code in {CODE_ATTEMPTS} draws")


@auth.require_role("merchant")
def redeem_voucher(code):
    if not config.CODE_PATTERN.fullmatch(code):
        return error(404)
    redeemed_at = to_text(now())
    conn = get_db()
    with db.write_transaction(conn):
        changed = conn.execute(
            "update vouchers set status = 'redeemed', redeemed_at = ?, redeemed_by = ?"
            " where code = ? and status = 'unredeemed' and expires_at > ?"
            " returning amount_cents, issued_at, expires_at",
            (redeemed_at, g.username, code, redeemed_at)).fetchall()
        if not changed:
            known = conn.execute("select 1 from vouchers where code = ?", (code,)).fetchone()
            return error(409 if known else 404)
        voucher = changed[0]
        amount = voucher["amount_cents"]
        db.append_event(conn, code, "redeemed", amount, redeemed_at, voucher["issued_at"],
                        voucher["expires_at"])
    return jsonify(code=code, status="redeemed", amount_cents=amount, redeemed_at=redeemed_at)


@auth.require_role("finance")
def graphql():
    status, body = schema.execute(json_body(), get_db(), current_app.config["WAREHOUSE"])
    response = jsonify(body)
    response.status_code = status
    return response
