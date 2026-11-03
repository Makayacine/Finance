"""Firehose on a laptop: read the stream folder, call the transform, write what Firehose writes.

    python local-development/firehose_local.py

The delivery stream you deploy reads the Kinesis stream, buffers records, invokes the transform
Lambda with a batch, writes the ``Ok`` records to S3 and has Redshift ``COPY`` them. This does the
first three of those against the folders ``--local`` uses:

    _localrun/stream/voucher-events/shard-*.jsonl     what the relay appended
        -> lambda_handler(event, None)                 Firehose's own event shape, a batch at a time
        -> _localrun/bucket/voucher-events/YYYY/MM/DD/HH/*.jsonl          the Ok records
           _localrun/bucket/processing-failed/YYYY/MM/DD/HH/*.jsonl       the rest, as they came

and ``warehouse_local.py`` does the ``COPY``. A checkpoint file records how many lines of each
shard have been delivered, the way the delivery stream keeps its place in each shard, so running
it again sends only what the relay has added since.

The handler's answer is held to Firehose's contract before anything is written: every
``recordId`` back exactly once, in order, each with a known ``result``. A transform that broke
that would have every batch fail on AWS, and it fails here first.
"""

import argparse
import base64
import contextlib
import io
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
LOCAL = PROJECT / "_localrun"
RESULTS = {"Ok", "ProcessingFailed", "Dropped"}


def load_handler():
    import importlib.util
    path = PROJECT / "firehose-lambda" / "lambda_function.py"
    spec = importlib.util.spec_from_file_location("lambda_function", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.lambda_handler


def read_new(stream_dir, checkpoint):
    """Every stream line not yet delivered, shard by shard, with its place in the shard."""
    pending = []
    for path in sorted(Path(stream_dir).glob("shard-*.jsonl")):
        done = checkpoint["shards"].get(path.name, 0)
        with path.open(encoding="utf-8") as handle:
            for number, line in enumerate(handle):
                if number >= done:
                    pending.append((path.name, json.loads(line)))
    return pending


def firehose_event(batch):
    return {
        "invocationId": str(uuid.uuid4()),
        "deliveryStreamArn": "arn:aws:firehose:local:000000000000:deliverystream/voucher-events",
        "sourceKinesisStreamArn": "arn:aws:kinesis:local:000000000000:stream/voucher-events",
        "region": "local",
        "records": [{
            "recordId": f"{shard}:{line['SequenceNumber']}",
            "approximateArrivalTimestamp": line["ApproximateArrivalTimestamp"],
            "data": base64.b64encode(line["Data"].encode("utf-8")).decode("ascii"),
            "kinesisRecordMetadata": {
                "sequenceNumber": line["SequenceNumber"], "subsequenceNumber": 0,
                "partitionKey": line["PartitionKey"], "shardId": shard,
                "approximateArrivalTimestamp": line["ApproximateArrivalTimestamp"],
            },
        } for shard, line in batch],
    }


def check_contract(event, answer):
    sent = [r["recordId"] for r in event["records"]]
    back = [r.get("recordId") for r in answer.get("records", [])]
    if back != sent:
        raise RuntimeError("the transform must return every recordId once, in order")
    for record in answer["records"]:
        if record.get("result") not in RESULTS or not isinstance(record.get("data"), str):
            raise RuntimeError(f"record {record['recordId']}: not a valid transform result")


def deliver(stream_dir=LOCAL / "stream" / "voucher-events", bucket_dir=LOCAL / "bucket",
            checkpoint_path=LOCAL / "firehose-checkpoint.json", handler=None,
            buffer_records=1000):
    handler = handler or load_handler()
    checkpoint_path = Path(checkpoint_path)
    checkpoint = (json.loads(checkpoint_path.read_text()) if checkpoint_path.exists()
                  else {"shards": {}, "batches": 0})
    pending = read_new(stream_dir, checkpoint)
    stats = {"records": 0, "ok": 0, "processing_failed": 0, "batches": 0, "objects": []}
    for start in range(0, len(pending), buffer_records):
        batch = pending[start:start + buffer_records]
        event = firehose_event(batch)
        with contextlib.redirect_stdout(io.StringIO()):  # the handler's log line is CloudWatch's
            answer = handler(event, None)
        check_contract(event, answer)

        checkpoint["batches"] += 1
        hour = datetime.now(timezone.utc).strftime("%Y/%m/%d/%H")
        # Unique per delivery, as Firehose's own object names are: a batch delivered twice --
        # after a lost checkpoint, say -- lands as a second object, not over the first.
        name = f"voucher-events-{checkpoint['batches']:06d}-{event['invocationId']}.jsonl"
        ok = [base64.b64decode(r["data"]) for r in answer["records"] if r["result"] == "Ok"]
        failed = [json.dumps({"recordId": r["recordId"], "result": r["result"],
                              "rawData": r["data"]}).encode() + b"\n"
                  for r in answer["records"] if r["result"] == "ProcessingFailed"]
        for prefix, lines in (("voucher-events", ok), ("processing-failed", failed)):
            if lines:
                target = Path(bucket_dir) / prefix / hour / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"".join(lines))
                if prefix == "voucher-events":
                    stats["objects"].append(target)
        for shard, _ in batch:
            checkpoint["shards"][shard] = checkpoint["shards"].get(shard, 0) + 1
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        checkpoint_path.write_text(json.dumps(checkpoint, indent=2))
        stats["records"] += len(batch)
        stats["ok"] += len(ok)
        stats["processing_failed"] += len(failed)
        stats["batches"] += 1
    return stats


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--stream-dir", default=str(LOCAL / "stream" / "voucher-events"))
    parser.add_argument("--bucket-dir", default=str(LOCAL / "bucket"))
    parser.add_argument("--checkpoint", default=str(LOCAL / "firehose-checkpoint.json"))
    parser.add_argument("--buffer-records", type=int, default=1000)
    args = parser.parse_args(argv)
    stats = deliver(args.stream_dir, args.bucket_dir, args.checkpoint,
                    buffer_records=args.buffer_records)
    print(f"{stats['records']:,} record(s) in {stats['batches']} batch(es): {stats['ok']:,} Ok,"
          f" {stats['processing_failed']:,} ProcessingFailed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
