"""Publish the outbox to the stream: in order, a batch at a time, marked only once accepted.

    python kinesis-producer/outbox-relay.py --local
    python kinesis-producer/outbox-relay.py --db <service-db> --stream-name <stream> --region <region>

This is the lab's producer. Where the lab called ``put_record`` from a loop of made-up rows, the
relay reads what the service has already committed -- the ``outbox`` table, oldest first -- and
sends it with ``put_records``, up to 500 rows a call, with the voucher code as the partition key.
A row is marked ``published_at`` only after the stream has accepted it.

AT LEAST ONCE, ON PURPOSE
-------------------------

Two failures can put an event on the stream twice, and neither can be designed away:

*   the relay dies after ``put_records`` returns and before the rows are marked, so the next run
    sends them again;
*   ``put_records`` accepts some records of a call and throttles others -- the response says
    which, record by record -- and the throttled ones go again on the next pass, after records
    that were behind them in the outbox.

The alternative to a duplicate is a lost event, and a lost redemption is a liability the
warehouse never stops reporting. So the relay never marks a row it cannot prove was accepted, and
the warehouse dedupes on ``event_id`` and orders by ``occurred_at`` rather than by arrival. The
partition key keeps one voucher's events on one shard, which is as much ordering as the stream
can promise; nothing downstream relies on more.

Run one relay at a time. Two would not lose anything, but they would publish every row twice.

THE TWO STREAMS
---------------

``KinesisStream`` is the AWS path; boto3 is imported inside it, so ``--local`` and the tests never
import it. ``FolderStream`` stands in for it: one JSONL file per shard, a line per record, with
the shard chosen the way Kinesis chooses it -- the MD5 of the partition key as a 128-bit number,
cut into equal ranges. Each line holds what ``put_records`` was given, ``PartitionKey`` and
``Data``, plus the sequence number and arrival time the stream would add.
"""

import argparse
import contextlib
import hashlib
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from voucher_service import db  # noqa: E402
from voucher_service.timeutil import to_text, utc_now  # noqa: E402

LOCAL_DB = PROJECT / "_localrun" / "voucher-service.db"
LOCAL_STREAM = PROJECT / "_localrun" / "stream" / "voucher-events"
EVENT_FIELDS = ("event_id", "voucher_code", "event_type", "amount_cents", "occurred_at",
                "issued_at", "expires_at")
MAX_RECORDS_PER_CALL = 500  # the put_records limit


@dataclass(frozen=True)
class Record:
    partition_key: str
    data: bytes


def to_record(row):
    """One outbox row as a stream record: the event as compact JSON, keyed by its voucher."""
    event = {field: row[field] for field in EVENT_FIELDS}
    data = json.dumps(event, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return Record(row["voucher_code"], data)


def shard_for(partition_key, shards):
    digest = int.from_bytes(hashlib.md5(partition_key.encode("utf-8")).digest(), "big")
    return digest * shards >> 128


class FolderStream:
    """A folder standing in for a Kinesis stream: ``shard-000.jsonl``, ``shard-001.jsonl``, ..."""

    def __init__(self, root, shards=2):
        self.root = Path(root)
        self.shards = shards
        self.root.mkdir(parents=True, exist_ok=True)
        self._next = [self._lines(shard) for shard in range(shards)]

    def path(self, shard):
        return self.root / f"shard-{shard:03d}.jsonl"

    def _lines(self, shard):
        path = self.path(shard)
        if not path.exists():
            return 0
        with path.open("rb") as handle:
            return sum(1 for _ in handle)

    def put_records(self, records):
        arrival = int(time.time() * 1000)
        lines = {shard: [] for shard in range(self.shards)}
        for record in records:
            shard = shard_for(record.partition_key, self.shards)
            sequence = f"{shard:03d}{self._next[shard]:018d}"
            self._next[shard] += 1
            lines[shard].append(json.dumps({
                "SequenceNumber": sequence, "PartitionKey": record.partition_key,
                "ApproximateArrivalTimestamp": arrival, "Data": record.data.decode("utf-8"),
            }, separators=(",", ":")))
        for shard, batch in lines.items():
            if batch:
                with self.path(shard).open("a", encoding="utf-8") as handle:
                    handle.write("\n".join(batch) + "\n")
        return [True] * len(records)


class KinesisStream:
    def __init__(self, stream_name, region=None, client=None):
        if client is None:
            import boto3
            client = boto3.client("kinesis", region_name=region)
        self.client = client
        self.stream_name = stream_name

    def put_records(self, records):
        """One ``put_records`` call; True for each record the stream accepted."""
        response = self.client.put_records(
            StreamName=self.stream_name,
            Records=[{"Data": r.data, "PartitionKey": r.partition_key} for r in records])
        return ["ErrorCode" not in entry for entry in response["Records"]]


@dataclass
class RelayResult:
    published: int
    calls: int
    rejected: int
    remaining: int
    gave_up: bool


def relay(conn, stream, batch_size=MAX_RECORDS_PER_CALL, max_attempts=5, sleep=time.sleep,
          clock=utc_now):
    """Drain the outbox into ``stream``. Stops when it is empty, or after ``max_attempts`` passes
    in a row in which the stream refused something; the refused rows stay unpublished."""
    batch_size = min(batch_size, MAX_RECORDS_PER_CALL)
    published = calls = rejected = failed_passes = 0
    while True:
        rows = conn.execute(
            "select outbox_id, " + ", ".join(EVENT_FIELDS) + " from outbox"
            " where published_at is null order by outbox_id limit ?", (batch_size,)).fetchall()
        if not rows:
            break
        accepted = stream.put_records([to_record(row) for row in rows])
        calls += 1
        if len(accepted) != len(rows):
            raise RuntimeError("the stream answered for a different number of records")
        done = [row["outbox_id"] for row, ok in zip(rows, accepted) if ok]
        if done:
            stamp = to_text(clock())
            with db.write_transaction(conn):
                conn.executemany(
                    "update outbox set published_at = ? where outbox_id = ?"
                    " and published_at is null", [(stamp, outbox_id) for outbox_id in done])
            published += len(done)
        if len(done) == len(rows):
            failed_passes = 0
            continue
        rejected += len(rows) - len(done)
        failed_passes += 1
        if failed_passes >= max_attempts:
            break
        sleep(min(0.2 * 2 ** failed_passes, 10.0))
    remaining = conn.execute(
        "select count(*) from outbox where published_at is null").fetchone()[0]
    return RelayResult(published, calls, rejected, remaining, failed_passes >= max_attempts)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--local", action="store_true",
                        help="append to a folder standing in for the stream")
    parser.add_argument("--db", default=str(LOCAL_DB))
    parser.add_argument("--stream-dir", default=str(LOCAL_STREAM))
    parser.add_argument("--shards", type=int, default=2)
    parser.add_argument("--stream-name")
    parser.add_argument("--region")
    parser.add_argument("--batch-size", type=int, default=MAX_RECORDS_PER_CALL)
    args = parser.parse_args(argv)

    if args.local:
        stream = FolderStream(args.stream_dir, args.shards)
    elif args.stream_name:
        stream = KinesisStream(args.stream_name, args.region)
    else:
        parser.error("give --local, or --stream-name for Kinesis")
    with contextlib.closing(db.connect(args.db)) as conn:
        result = relay(conn, stream, batch_size=args.batch_size)
    print(f"published {result.published:,} event(s) in {result.calls:,} call(s);"
          f" {result.rejected:,} record(s) refused and retried; {result.remaining:,} unpublished")
    return 1 if result.gave_up else 0


if __name__ == "__main__":
    sys.exit(main())
