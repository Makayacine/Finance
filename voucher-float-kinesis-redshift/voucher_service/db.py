"""The service database: four tables, one connection per request, and the outbox writer.

    users              username, role, Argon2id hash -- seeded from config at start-up
    vouchers           the current state of every voucher; the only table the API mutates
    outbox             one row per state change, written in the same transaction as the change
    idempotency_keys   the first 201 for each (issuer, Idempotency-Key), replayed on a retry

WHY AN OUTBOX
-------------

The service has to change a voucher and tell the stream it changed. Doing both directly is a
dual write: a crash between the commit and ``put_record`` loses the event, and a crash the other
way round publishes a redemption that never happened. So the request only writes to its own
database -- the voucher row and an outbox row, in one transaction, both or neither -- and the
relay (``kinesis-producer/outbox-relay.py``) publishes the outbox afterwards, marking a row only
once the stream has accepted it. The stream can then see an event twice, never zero times, and
the warehouse dedupes on ``event_id``.

TRANSACTIONS
------------

Connections run in autocommit mode and every write goes through ``write_transaction``, which
opens with ``BEGIN IMMEDIATE``. That takes SQLite's write lock at the start rather than at the
first write, so the idempotency lookup and the insert it guards cannot interleave with another
request's; a second writer waits on the busy timeout instead of failing. The database runs in
WAL mode so readers never wait on a writer.
"""

import contextlib
import sqlite3
import uuid

SCHEMA = """
create table if not exists users (
    username       text primary key,
    role           text not null check (role in ('issuer', 'merchant', 'finance')),
    password_hash  text not null
);

create table if not exists vouchers (
    code           text primary key,
    recipient_id   text not null,
    amount_cents   integer not null check (amount_cents > 0),
    status         text not null check (status in ('unredeemed', 'redeemed', 'expired')),
    issued_at      text not null,
    expires_at     text not null,
    issued_by      text not null,
    redeemed_at    text,
    redeemed_by    text,
    swept_at       text
);
create index if not exists vouchers_open on vouchers (status, expires_at);

create table if not exists outbox (
    outbox_id      integer primary key autoincrement,
    event_id       text not null unique,
    voucher_code   text not null references vouchers (code),
    event_type     text not null check (event_type in ('issued', 'redeemed', 'expired')),
    amount_cents   integer not null,
    occurred_at    text not null,
    expires_at     text not null,
    published_at   text
);
create index if not exists outbox_unpublished on outbox (outbox_id) where published_at is null;

create table if not exists idempotency_keys (
    username       text not null,
    idempotency_key text not null,
    request_hash   text not null,
    status_code    integer not null,
    response_body  text not null,
    created_at     text not null,
    primary key (username, idempotency_key)
);
"""

BUSY_TIMEOUT_SECONDS = 10


def connect(path):
    conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma foreign_keys = on")
    return conn


def create_schema(path):
    with contextlib.closing(connect(path)) as conn:
        conn.execute("pragma journal_mode = wal")
        conn.executescript(SCHEMA)


@contextlib.contextmanager
def write_transaction(conn):
    """``BEGIN IMMEDIATE`` ... ``COMMIT``, or ``ROLLBACK`` if anything inside raises."""
    conn.execute("begin immediate")
    try:
        yield conn
    except BaseException:
        conn.execute("rollback")
        raise
    conn.execute("commit")


def append_event(conn, voucher_code, event_type, amount_cents, occurred_at, expires_at):
    """Write one outbox row. Call it inside the transaction that made the change it records."""
    event_id = str(uuid.uuid4())
    conn.execute(
        "insert into outbox (event_id, voucher_code, event_type, amount_cents, occurred_at,"
        " expires_at) values (?, ?, ?, ?, ?, ?)",
        (event_id, voucher_code, event_type, amount_cents, occurred_at, expires_at),
    )
    return event_id
