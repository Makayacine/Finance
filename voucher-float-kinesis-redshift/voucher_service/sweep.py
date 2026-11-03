"""The expiry sweep: mark every overdue unredeemed voucher expired, with one outbox row each.

    python -m voucher_service sweep --db _localrun/voucher-service.db
    python -m voucher_service sweep --db _localrun/voucher-service.db --now 2026-12-01T00:00:00Z

A voucher stops being redeemable the second its ``expires_at`` passes -- the redemption guard
checks the time itself -- so the sweep decides nothing about who may spend what. What it does is
make the lapse an event: until the sweep runs, the voucher's row still says ``unredeemed`` and
the stream has never heard that the liability is gone.

TWO TIMES, KEPT APART
---------------------

The ``expired`` event's ``occurred_at`` is the voucher's ``expires_at``, not the time the sweep
ran. The right to spend the voucher lapsed at expiry; the sweep only noticed. Stamping the sweep
time would make the warehouse's daily liability depend on how often a scheduler fires, and a
sweep that missed a weekend would leave three days of breakage booked on Monday. The sweep time
is kept on the voucher row as ``swept_at``, which is where an operator would look for it.

The UPDATE and the outbox rows share one transaction, and ``returning`` hands back exactly the
rows the UPDATE changed, so a voucher redeemed a moment before the sweep is never also expired.
Running it twice in a row expires nothing the second time.
"""

from . import db
from .timeutil import to_text


def sweep(conn, now):
    """Expire what is overdue at ``now``; returns how many vouchers were expired."""
    cutoff = to_text(now)
    with db.write_transaction(conn):
        rows = conn.execute(
            "update vouchers set status = 'expired', swept_at = ?"
            " where status = 'unredeemed' and expires_at <= ?"
            " returning code, amount_cents, expires_at", (cutoff, cutoff)).fetchall()
        # RETURNING has no defined order; the outbox's should not depend on the query plan.
        for row in sorted(rows, key=lambda r: (r["expires_at"], r["code"])):
            db.append_event(conn, row["code"], "expired", row["amount_cents"],
                            row["expires_at"], row["expires_at"])
    return len(rows)
