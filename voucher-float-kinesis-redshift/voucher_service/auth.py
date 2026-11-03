"""Argon2id for passwords, a short-lived HS256 JWT for everything after login, one role per user.

    seed(conn, users)          write the configured users, hashing each password with Argon2id
    check_password(...)        the role on a match, None otherwise -- in the same time either way
    issue_token / read_token   the JWT carries ``sub`` and ``role``
    require_role(*roles)       401 without a valid token, 403 with the wrong role

Errors say as little as possible. A wrong password and an unknown user get the same 401 with the
same body, and an unknown user still costs one Argon2 verification against a fixed hash, so the
response time does not say which usernames exist either. A token that is missing, malformed,
expired, signed with another key or signed with ``alg: none`` is one 401; PyJWT is told the only
algorithm it may accept, which is what closes the ``none`` and key-confusion tricks.
"""

import functools
import time

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from flask import current_app, g, request

from .config import ROLES
from .errors import error

ALGORITHM = "HS256"
HASHER = PasswordHasher()  # argon2-cffi's defaults: Argon2id, RFC 9106's low-memory profile
_UNKNOWN_USER_HASH = HASHER.hash("no user has this password")


def seed(conn, users):
    """Upsert each configured user. A stored hash is kept while it still matches the password."""
    for user in users:
        if user.role not in ROLES:
            raise ValueError(f"unknown role {user.role!r}")
        row = conn.execute("select password_hash from users where username = ?",
                           (user.username,)).fetchone()
        if row is not None and _matches(row["password_hash"], user.password) \
                and not HASHER.check_needs_rehash(row["password_hash"]):
            conn.execute("update users set role = ? where username = ?",
                         (user.role, user.username))
            continue
        conn.execute(
            "insert into users (username, role, password_hash) values (?, ?, ?)"
            " on conflict (username) do update set role = excluded.role,"
            " password_hash = excluded.password_hash",
            (user.username, user.role, HASHER.hash(user.password)),
        )


def _matches(password_hash, password):
    try:
        return HASHER.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def check_password(conn, username, password):
    row = conn.execute("select role, password_hash from users where username = ?",
                       (username,)).fetchone()
    if row is None:
        _matches(_UNKNOWN_USER_HASH, password)  # spend the same time an existing user would
        return None
    return row["role"] if _matches(row["password_hash"], password) else None


def issue_token(username, role, secret, ttl_seconds):
    now = int(time.time())
    claims = {"sub": username, "role": role, "iat": now, "exp": now + ttl_seconds}
    return jwt.encode(claims, secret, algorithm=ALGORITHM)


def read_token(token, secret):
    """The claims of a valid token, or None."""
    try:
        claims = jwt.decode(token, secret, algorithms=[ALGORITHM],
                            options={"require": ["sub", "role", "iat", "exp"]})
    except jwt.PyJWTError:
        return None
    if claims.get("role") not in ROLES or not isinstance(claims.get("sub"), str):
        return None
    return claims


def bearer_claims():
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return read_token(token.strip(), current_app.config["JWT_SECRET"])


def unauthenticated():
    response = error(401)
    response.headers["WWW-Authenticate"] = "Bearer"
    return response


def require_role(*allowed):
    """Authenticate first, then authorise: no token is 401, the wrong role is 403."""
    def decorate(view):
        @functools.wraps(view)
        def guarded(*args, **kwargs):
            claims = bearer_claims()
            if claims is None:
                return unauthenticated()
            if claims["role"] not in allowed:
                return error(403)
            g.username, g.role = claims["sub"], claims["role"]
            return view(*args, **kwargs)
        return guarded
    return decorate
