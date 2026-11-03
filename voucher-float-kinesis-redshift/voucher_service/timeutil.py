"""Time as the service stores it, and a clock the simulator can move.

Every timestamp is UTC text in one fixed-width form, ``2026-01-15T09:00:00Z``. Fixed width is the
point: SQLite has no timestamp type, so ``expires_at > ?`` in the redemption guard is a string
comparison, and a string comparison orders time correctly only when every value has the same
shape. Seconds are the resolution; nothing in a voucher's life needs finer.

The service never calls ``datetime.now()`` directly. It asks the clock it was built with, which
is ``utc_now`` in production and a ``SettableClock`` in the simulator and the tests, so two years
of vouchers can be issued, redeemed and swept in a few minutes against the same code paths.
JWT expiry is the one exception, and deliberately: a login token is checked against wall-clock
time by PyJWT, because it guards the real request, not the simulated business day.
"""

from datetime import datetime, timedelta, timezone

FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def to_text(moment):
    if moment.tzinfo is None:
        raise ValueError("a naive datetime has no place in the ledger")
    return moment.astimezone(timezone.utc).strftime(FORMAT)


def from_text(text):
    return datetime.strptime(text, FORMAT).replace(tzinfo=timezone.utc)


class SettableClock:
    """A clock that reads what it was last set to."""

    def __init__(self, start):
        self.moment = start

    def __call__(self):
        return self.moment

    def set(self, moment):
        self.moment = moment

    def advance(self, **delta):
        self.moment += timedelta(**delta)
