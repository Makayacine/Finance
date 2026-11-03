"""Two years of a voucher programme, driven through the service's own REST endpoints.

    python local-development/simulate.py                 # about 50,000 vouchers, 1-2 minutes
    python local-development/simulate.py --scale 0.05    # a twentieth of the volume

WHY THE DATA IS SIMULATED
-------------------------

The question this project answers -- how much of today's voucher float will never be redeemed --
has a right answer only in a world whose rules are known. Real voucher books do not publish their
customers' intentions, so a breakage estimator run on real data can only be compared with
another estimate. Here the rules are fixed before the first voucher is issued: 18% of recipients
never redeem, the rest redeem after a Weibull delay with shape 0.8 and scale 9 days, and a
voucher lapses after 30 days. That makes the true breakage rate a number,

    0.18 + 0.82 * exp(-(30 / 9) ** 0.8)  =  0.2397

and the warehouse's estimate for a cohort that is only days old can be checked against it. The
simulator writes every parameter, that rate and the true redemption curve to
``data/simulation-truth.json``, and the notebook reads them from there.

WHAT IT DRIVES
--------------

Nothing is written to the database directly. Every voucher is issued with ``POST /vouchers`` as
the issuer, every redemption is attempted with ``POST /vouchers/<code>/redeem`` as a merchant,
through Flask's test client, and the expiry sweep runs every night at 01:00 UTC on the service's
own clock, which the simulator moves forward event by event. So the data in the outbox is what the
service produced, through its own guards:

*   **Issue pattern.** A Poisson process over 07:00-19:00 UTC, 72 vouchers a weekday on average,
    0.55x on Saturdays and 0.35x on Sundays, and 2x on the last three business days of every
    month, when corporate reward runs go out.
*   **Amounts.** Six denominations from 50.00 to 1,000.00, and 5% store credit to the cent.
*   **Redemption.** Recipients who redeem try to after their Weibull delay. A delay of 30 days or
    more means the voucher has lapsed by then: the attempt is still made, and the service must
    answer 409.
*   **Retries.** 2% of issue requests are sent twice with the same Idempotency-Key, as a client
    whose first response was lost would; the second must return the first voucher.
*   **A sweep outage.** The sweep does not run from 9 to 15 March 2026 and catches up on the 16th,
    so a week of expiries reaches the stream dated a week before the events around them.

Every response is checked against what the plan says it must be -- 201, a replay of the same
code, 200 before expiry and 409 from it -- and the run stops at the first one that is not.
"""

import argparse
import calendar
import contextlib
import json
import math
import random
import sys
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from voucher_service import config, db  # noqa: E402
from voucher_service.app import create_app  # noqa: E402
from voucher_service.sweep import sweep  # noqa: E402
from voucher_service.timeutil import SettableClock, from_text, to_text  # noqa: E402

LOCAL_DB = PROJECT / "_localrun" / "voucher-service.db"
TRUTH = PROJECT / "data" / "simulation-truth.json"


@dataclass(frozen=True)
class Parameters:
    seed: int = 20241104
    start: str = "2024-11-04T00:00:00Z"
    days: int = 728
    scale: float = 1.0
    weekday_rate: float = 72.0
    saturday_factor: float = 0.55
    sunday_factor: float = 0.35
    month_end_business_days: int = 3
    month_end_factor: float = 2.0
    open_hour_utc: int = 7
    close_hour_utc: int = 19
    denominations_cents: tuple = ((5000, 0.20), (10000, 0.30), (20000, 0.20), (25000, 0.10),
                                  (50000, 0.12), (100000, 0.03))
    store_credit_share: float = 0.05
    store_credit_range_cents: tuple = (500, 30000)
    never_redeem_share: float = 0.18
    delay_distribution: str = "weibull"
    delay_shape: float = 0.8
    delay_scale_days: float = 9.0
    term_days: int = config.TERM_DAYS
    retry_share: float = 0.02
    sweep_hour_utc: int = 1
    sweep_outage: tuple = ("2026-03-09", "2026-03-15")


def true_redeemed_within(p, days):
    """The share of issued value redeemed within ``days`` of issue, before any expiry."""
    within = min(days, p.term_days)
    return (1 - p.never_redeem_share) * (1 - math.exp(-((within / p.delay_scale_days)
                                                         ** p.delay_shape)))


def true_breakage(p):
    return 1 - true_redeemed_within(p, p.term_days)


def month_end_days(year, month, count):
    last = calendar.monthrange(year, month)[1]
    business = [date(year, month, d) for d in range(last, 0, -1)
                if date(year, month, d).weekday() < 5]
    return set(business[:count])


def day_rate(p, day):
    rate = p.weekday_rate * p.scale
    if day.weekday() == 5:
        rate *= p.saturday_factor
    elif day.weekday() == 6:
        rate *= p.sunday_factor
    if day in month_end_days(day.year, day.month, p.month_end_business_days):
        rate *= p.month_end_factor
    return rate


@dataclass
class Voucher:
    index: int
    issued_at: datetime
    amount_cents: int
    redeems: bool
    delay_seconds: int | None
    retried: bool


def plan(p):
    """Every voucher the simulation will issue, in issue order, drawn from one seeded stream."""
    rng = random.Random(p.seed)
    start = from_text(p.start)
    hours = p.close_hour_utc - p.open_hour_utc
    values = [v for v, _ in p.denominations_cents]
    weights = [w for _, w in p.denominations_cents]
    vouchers = []
    for offset in range(p.days):
        day = start + timedelta(days=offset)
        per_hour = day_rate(p, day.date()) / hours
        moment = rng.expovariate(per_hour)
        while moment < hours:
            issued_at = day + timedelta(hours=p.open_hour_utc, seconds=int(moment * 3600))
            if rng.random() < p.store_credit_share:
                amount = rng.randint(*p.store_credit_range_cents)
            else:
                amount = rng.choices(values, weights)[0]
            redeems = rng.random() >= p.never_redeem_share
            delay = rng.weibullvariate(p.delay_scale_days, p.delay_shape) if redeems else None
            vouchers.append(Voucher(len(vouchers), issued_at, amount, redeems,
                                    None if delay is None else int(delay * 86400),
                                    rng.random() < p.retry_share))
            moment += rng.expovariate(per_hour)
    return vouchers


ISSUE, RETRY, REDEEM, SWEEP = 0, 1, 2, 3


def schedule(p, vouchers):
    start = from_text(p.start)
    as_of = start + timedelta(days=p.days)
    first, last = (date.fromisoformat(d) for d in p.sweep_outage)
    outage = {first + timedelta(days=i) for i in range((last - first).days + 1)}
    actions = []
    for v in vouchers:
        actions.append((v.issued_at, ISSUE, v.index))
        if v.retried:
            actions.append((v.issued_at + timedelta(seconds=7), RETRY, v.index))
        if v.redeems:
            attempt = v.issued_at + timedelta(seconds=v.delay_seconds)
            if attempt < as_of:
                actions.append((attempt, REDEEM, v.index))
    for offset in range(1, p.days):
        moment = start + timedelta(days=offset, hours=p.sweep_hour_utc)
        if moment.date() not in outage:
            actions.append((moment, SWEEP, -1))
    actions.append((as_of, SWEEP, -1))
    actions.sort(key=lambda a: (a[0], a[1], a[2]))
    return actions, as_of


class SimulationError(AssertionError):
    pass


def simulate(db_path, p=Parameters(), truth_path=None, progress=None):
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        Path(f"{db_path}{suffix}").unlink(missing_ok=True)

    vouchers = plan(p)
    actions, as_of = schedule(p, vouchers)
    term = timedelta(days=p.term_days)
    clock = SettableClock(from_text(p.start))
    code_rng = random.Random(p.seed + 1)
    draws = [0]

    def next_code():
        draws[0] += 1
        return f"{config.CODE_PREFIX}{code_rng.getrandbits(32):08x}"

    app = create_app(db_path, clock=clock, code_factory=next_code)
    client = app.test_client()
    passwords = {u.username: u.password for u in config.seed_users()}

    def token(role):
        response = client.post("/login", json={"username": role, "password": passwords[role]})
        return {"Authorization": "Bearer " + response.get_json()["access_token"]}

    headers = {"issuer": token("issuer"), "merchant": token("merchant")}
    codes = {}
    tally = dict(issued=0, issued_cents=0, replays=0, redeemed=0, redeemed_cents=0,
                 refused_lapsed=0, expired=0, sweeps=0)
    started = time.monotonic()
    conn = db.connect(db_path)
    try:
        for n, (moment, kind, index) in enumerate(actions):
            clock.set(moment)
            if kind == SWEEP:
                tally["expired"] += sweep(conn, moment)
                tally["sweeps"] += 1
                continue
            v = vouchers[index]
            if kind in (ISSUE, RETRY):
                response = client.post(
                    "/vouchers", json={"amount_cents": v.amount_cents,
                                       "recipient_id": f"cust-{v.index:06d}"},
                    headers={**headers["issuer"], "Idempotency-Key": f"sim-{v.index:06d}"})
                expect(response.status_code == 201, v, "issue", response)
                code = response.get_json()["code"]
                if kind == ISSUE:
                    codes[index] = code
                    tally["issued"] += 1
                    tally["issued_cents"] += v.amount_cents
                else:
                    expect(code == codes[index]
                           and response.headers.get("Idempotent-Replayed") == "true",
                           v, "replay", response)
                    tally["replays"] += 1
            else:
                response = client.post(f"/vouchers/{codes[index]}/redeem",
                                       headers=headers["merchant"])
                lapsed = moment >= v.issued_at + term
                expect(response.status_code == (409 if lapsed else 200), v, "redeem", response)
                if lapsed:
                    tally["refused_lapsed"] += 1
                else:
                    tally["redeemed"] += 1
                    tally["redeemed_cents"] += v.amount_cents
            if progress and n % 20000 == 0:
                progress(f"{n:,}/{len(actions):,} actions, simulated {to_text(moment)}")
    finally:
        conn.close()

    with contextlib.closing(db.connect(db_path)) as check:
        open_cents, open_count = check.execute(
            "select coalesce(sum(amount_cents), 0), count(*) from vouchers"
            " where status = 'unredeemed'").fetchone()
        outbox = check.execute("select count(*) from outbox").fetchone()[0]

    truth = {
        "parameters": asdict(p),
        "as_of": to_text(as_of),
        "true_breakage_rate": round(true_breakage(p), 6),
        "true_redeemed_within_days": {str(d): round(true_redeemed_within(p, d), 6)
                                      for d in range(p.term_days + 1)},
        "realised": {
            **tally,
            "open_at_as_of": open_count,
            "open_cents_at_as_of": open_cents,
            "outbox_events": outbox,
            "code_draws": draws[0],
            "code_collisions_redrawn": draws[0] - tally["issued"],
        },
    }
    if truth_path:
        Path(truth_path).parent.mkdir(parents=True, exist_ok=True)
        Path(truth_path).write_text(json.dumps(truth, indent=2) + "\n", encoding="utf-8")
    truth["seconds"] = round(time.monotonic() - started, 1)
    return truth


def expect(condition, voucher, step, response):
    if not condition:
        raise SimulationError(f"voucher {voucher.index}: {step} answered "
                              f"{response.status_code} {response.get_data(as_text=True)!r}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", default=str(LOCAL_DB))
    parser.add_argument("--truth", default=str(TRUTH))
    parser.add_argument("--scale", type=float, default=1.0)
    parser.add_argument("--days", type=int, default=Parameters.days)
    args = parser.parse_args(argv)
    p = Parameters(scale=args.scale, days=args.days)
    truth = simulate(args.db, p, args.truth, progress=print)
    r = truth["realised"]
    print(f"{r['issued']:,} vouchers, {r['issued_cents'] / 100:,.2f} issued;"
          f" {r['redeemed']:,} redeemed, {r['expired']:,} expired, {r['open_at_as_of']:,} open"
          f" at {truth['as_of']}; {r['refused_lapsed']:,} lapsed attempts refused,"
          f" {r['replays']:,} retries replayed, {r['code_collisions_redrawn']} code(s) redrawn;"
          f" {r['outbox_events']:,} outbox events in {truth['seconds']} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
