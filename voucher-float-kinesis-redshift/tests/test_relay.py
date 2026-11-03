"""The relay: in outbox order, marked only once the stream has the record, and never a lost event.

Most of these run against ``FolderStream``, the same class ``--local`` uses. Three hand
``KinesisStream`` -- the AWS class itself -- a client that answers ``put_records`` the way Kinesis
does and keeps what it was sent: the stream name, and each record's ``Data`` and
``PartitionKey``. Two of them make it throttle, with a ``FailedRecordCount`` and an ``ErrorCode``
on each refused entry. That is the response shape the relay has to read correctly.

The crash test makes SQLite refuse the "mark published" update after the stream has accepted the
batch. That is the window that turns at-least-once into duplicates, and the test checks the
duplicates carry the same ``event_id`` so the warehouse can drop them.
"""

import json
import sqlite3
from collections import Counter, defaultdict

import pytest

from conftest import issue, redeem, relay_module, rows
from voucher_service import db

FolderStream, KinesisStream, relay = (relay_module.FolderStream, relay_module.KinesisStream,
                                      relay_module.relay)


@pytest.fixture
def busy(app, client, tokens):
    """Ten vouchers, four redeemed: fourteen outbox rows."""
    codes = [issue(client, tokens["issuer"], amount_cents=100 * (i + 1), key=f"k{i}")
             .get_json()["code"] for i in range(10)]
    for code in codes[::3]:
        assert redeem(client, tokens["merchant"], code).status_code == 200
    return codes


@pytest.fixture
def conn(app):
    connection = db.connect(app.config["DB_PATH"])
    yield connection
    connection.close()


def stream_lines(stream):
    lines = []
    for shard in range(stream.shards):
        path = stream.path(shard)
        if path.exists():
            lines += [json.loads(line) for line in path.read_text().splitlines()]
    return lines


def events(stream):
    return [json.loads(line["Data"]) for line in stream_lines(stream)]


def test_the_relay_publishes_every_row_in_outbox_order(app, conn, busy, tmp_path):
    stream = FolderStream(tmp_path / "stream", shards=1)
    result = relay(conn, stream)
    assert (result.published, result.remaining, result.gave_up) == (14, 0, False)
    outbox = rows(app, "select * from outbox order by outbox_id")
    assert [e["event_id"] for e in events(stream)] == [r["event_id"] for r in outbox]
    assert all(r["published_at"] for r in outbox)
    first = events(stream)[0]
    assert set(first) == {"event_id", "voucher_code", "event_type", "amount_cents",
                          "occurred_at", "issued_at", "expires_at"}
    assert first == {k: outbox[0][k] for k in first}


def test_a_second_run_publishes_nothing(conn, busy, tmp_path):
    stream = FolderStream(tmp_path / "stream")
    relay(conn, stream)
    again = relay(conn, stream)
    assert (again.published, again.calls) == (0, 0)
    assert len(stream_lines(stream)) == 14


def test_the_partition_key_keeps_a_vouchers_events_on_one_shard_in_order(conn, busy, tmp_path):
    stream = FolderStream(tmp_path / "stream", shards=4)
    relay(conn, stream)
    shards_by_code = defaultdict(set)
    for shard in range(4):
        path = stream.path(shard)
        for line in (path.read_text().splitlines() if path.exists() else []):
            record = json.loads(line)
            event = json.loads(record["Data"])
            assert record["PartitionKey"] == event["voucher_code"]
            shards_by_code[event["voucher_code"]].add(shard)
    assert all(len(shards) == 1 for shards in shards_by_code.values())
    assert len({s for shards in shards_by_code.values() for s in shards}) > 1
    for code in busy[::3]:
        mine = [e["event_type"] for e in events(stream) if e["voucher_code"] == code]
        assert mine == ["issued", "redeemed"]


def test_batches_never_exceed_the_batch_size(conn, busy, tmp_path):
    folder = FolderStream(tmp_path / "stream")
    sizes = []

    class Counting:
        def put_records(self, records):
            sizes.append(len(records))
            return folder.put_records(records)

    result = relay(conn, Counting(), batch_size=4)
    assert sizes == [4, 4, 4, 2] and result.published == 14


class ThrottlingKinesis:
    """Answers ``put_records`` as Kinesis does, refusing the records ``refuse`` picks."""

    def __init__(self, refuse):
        self.refuse = refuse
        self.accepted = []
        self.requests = []
        self.calls = 0

    def put_records(self, StreamName, Records):
        self.calls += 1
        self.requests.append((StreamName, Records))
        entries = []
        for position, record in enumerate(Records):
            if self.refuse(self.calls, position):
                entries.append({"ErrorCode": "ProvisionedThroughputExceededException",
                                "ErrorMessage": "Rate exceeded for shard"})
            else:
                self.accepted.append(json.loads(record["Data"]))
                entries.append({"SequenceNumber": str(len(self.accepted)),
                                "ShardId": "shardId-000000000000"})
        failed = sum("ErrorCode" in entry for entry in entries)
        return {"FailedRecordCount": failed, "Records": entries}


def test_refused_records_stay_unpublished_and_go_again(app, conn, busy):
    client = ThrottlingKinesis(refuse=lambda call, position: call == 1 and position % 3 == 1)
    naps = []
    result = relay(conn, KinesisStream("voucher-events", client=client), sleep=naps.append)
    assert result.published == 14 and result.remaining == 0 and result.rejected == 5
    assert len(naps) == 1
    sent = Counter(e["event_id"] for e in client.accepted)
    assert set(sent) == {r["event_id"] for r in rows(app, "select event_id from outbox")}
    assert set(sent.values()) == {1}


def test_kinesis_is_sent_the_event_as_data_keyed_by_its_voucher_code(app, conn, busy):
    client = ThrottlingKinesis(refuse=lambda call, position: False)
    relay(conn, KinesisStream("voucher-events", client=client))
    [(stream_name, records)] = client.requests
    assert stream_name == "voucher-events" and len(records) == 14
    outbox = rows(app, "select * from outbox order by outbox_id")
    for record, row in zip(records, outbox):
        assert set(record) == {"Data", "PartitionKey"} and isinstance(record["Data"], bytes)
        assert record["PartitionKey"] == row["voucher_code"]
        assert json.loads(record["Data"])["event_id"] == row["event_id"]


def test_a_call_never_carries_more_than_the_500_kinesis_takes(app, conn, tmp_path):
    """Asked for bigger batches, the relay still sends 500 at most: Kinesis rejects a
    ``put_records`` call of more than 500 records outright."""
    with db.write_transaction(conn):
        for n in range(1201):
            code = f"VCH-{n:08x}"
            conn.execute("insert into vouchers (code, recipient_id, amount_cents, status,"
                         " issued_at, expires_at, issued_by) values (?, 'c', 100, 'unredeemed',"
                         " '2026-01-15T09:00:00Z', '2026-02-14T09:00:00Z', 'issuer')", (code,))
            db.append_event(conn, code, "issued", 100, "2026-01-15T09:00:00Z",
                            "2026-01-15T09:00:00Z", "2026-02-14T09:00:00Z")
    folder = FolderStream(tmp_path / "stream")
    sizes = []

    class Counting:
        def put_records(self, records):
            sizes.append(len(records))
            return folder.put_records(records)

    result = relay(conn, Counting(), batch_size=5000)
    assert sizes == [500, 500, 201] and result.published == 1201


def test_a_stream_that_keeps_refusing_is_given_up_on_with_nothing_marked(app, conn, busy):
    client = ThrottlingKinesis(refuse=lambda call, position: True)
    result = relay(conn, KinesisStream("voucher-events", client=client), max_attempts=3,
                   sleep=lambda seconds: None)
    assert result.gave_up and result.published == 0 and result.remaining == 14
    assert client.calls == 3
    assert rows(app, "select count(*) as n from outbox where published_at is not null") == \
        [{"n": 0}]


def test_a_crash_before_marking_resends_and_the_copies_share_an_event_id(app, conn, busy,
                                                                          tmp_path):
    stream = FolderStream(tmp_path / "stream")
    blocker = sqlite3.connect(app.config["DB_PATH"])
    blocker.execute("create trigger relay_dies before update of published_at on outbox"
                    " begin select raise(abort, 'relay died before marking'); end")
    blocker.commit()
    with pytest.raises(sqlite3.IntegrityError):
        relay(conn, stream)
    assert len(stream_lines(stream)) == 14  # the stream has the batch...
    assert rows(app, "select count(*) as n from outbox where published_at is null") == \
        [{"n": 14}]  # ...and the outbox does not know it
    blocker.execute("drop trigger relay_dies")
    blocker.commit()
    blocker.close()

    assert relay(conn, stream).published == 14
    copies = Counter(e["event_id"] for e in events(stream))
    assert len(copies) == 14 and set(copies.values()) == {2}


def test_the_relay_command(app, busy, tmp_path, capsys):
    stream_dir = tmp_path / "stream"
    code = relay_module.main(["--local", "--db", app.config["DB_PATH"],
                              "--stream-dir", str(stream_dir)])
    assert code == 0
    assert "published 14 event(s) in 1 call(s)" in capsys.readouterr().out
    assert len(stream_lines(FolderStream(stream_dir))) == 14


def test_the_shard_split_matches_kinesis_equal_ranges():
    assert relay_module.shard_for("VCH-00000000", 1) == 0
    spread = Counter(relay_module.shard_for(f"VCH-{i:08x}", 4) for i in range(4000))
    assert set(spread) == {0, 1, 2, 3}
    assert min(spread.values()) > 900
