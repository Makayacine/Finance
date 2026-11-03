"""Every setting the voucher service reads, in one place.

    CODE_PREFIX, CODE_PATTERN     a code is ``VCH-`` and 8 lowercase hex digits
    TERM_DAYS                     a voucher expires 30 days after it is issued
    MAX_AMOUNT_CENTS              the largest voucher the service will issue
    TOKEN_TTL_SECONDS             how long a login's JWT is good for
    SEED_USERS                    one user per role, seeded into the service database at start-up

MONEY IS INTEGER CENTS
----------------------

Every amount the service accepts, stores and emits is an integer number of cents. A JSON ``100.0``
is refused as firmly as ``"100"``: a float that happens to be whole today is a float that will
not be whole after somebody's currency conversion, and the liability the finance team reports is
a sum over hundreds of thousands of these. ``MAX_AMOUNT_CENTS`` is there for a plainer reason
too: SQLite stores a 64-bit integer, and Python will happily parse a JSON number that is not one.

USERS AND PASSWORDS
-------------------

The three users below are written into the service database every time it starts, and only an
Argon2id hash of each password is stored there. The plaintext comes from an environment variable
per user. The defaults beside them exist so the local run works straight after a clone; set the
variables for the one you deploy, and the next start replaces the stored hash.
"""

import os
import re
from dataclasses import dataclass

CODE_PREFIX = "VCH-"
CODE_PATTERN = re.compile(r"VCH-[0-9a-f]{8}")
TERM_DAYS = 30
MAX_AMOUNT_CENTS = 10_000_000  # 100,000.00 in the voucher's currency
TOKEN_TTL_SECONDS = 3600
MAX_REQUEST_BYTES = 16 * 1024

# An opaque customer reference, never an email or a name: the outbox does not carry it, and the
# service has no reason to hold anything a person could be found by.
RECIPIENT_PATTERN = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
# Printable ASCII, no spaces -- a UUID, a ULID or a client's own request id all fit.
IDEMPOTENCY_KEY_PATTERN = re.compile(r"[\x21-\x7e]{1,255}")

ROLES = ("issuer", "merchant", "finance")


@dataclass(frozen=True)
class SeedUser:
    username: str
    role: str
    password: str


# username, role, the environment variable that holds the password, the local default
_SEED = (
    ("issuer", "issuer", "VOUCHER_ISSUER_PASSWORD", "issuer-local-only"),
    ("merchant", "merchant", "VOUCHER_MERCHANT_PASSWORD", "merchant-local-only"),
    ("finance", "finance", "VOUCHER_FINANCE_PASSWORD", "finance-local-only"),
)


def seed_users(environ=os.environ):
    return [SeedUser(name, role, environ.get(variable) or default)
            for name, role, variable, default in _SEED]


def jwt_secret(environ=os.environ):
    """The signing key, or None when the environment does not set one.

    None makes ``create_app`` mint a random key for the life of the process: tokens then die
    with the server, which is the right failure for a laptop and the wrong one for production.
    """
    return environ.get("VOUCHER_JWT_SECRET") or None
