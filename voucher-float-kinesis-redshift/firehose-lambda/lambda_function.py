"""The Firehose transform: check every voucher event, derive its cohort and age, emit JSON lines.

Firehose calls ``lambda_handler`` with a batch of stream records and loads whatever comes back
``Ok`` into Redshift through S3 and ``COPY``. The contract is the lab's: read ``event['records']``,
base64-decode each ``record['data']``, and return every ``recordId`` exactly once with a
``result`` and base64 ``data``. Where the lab built CSV lines with f-strings, this emits one JSON
object per line: a comma in a field cannot shift a column, and ``COPY ... FORMAT JSON 'auto'``
matches keys to columns by name, so the column order is nobody's business.

WHAT IT CHECKS
--------------

Exactly the seven fields the relay sends, each of the right type and shape, and the timestamps in
the order a voucher's life allows:

    issued     occurred_at == issued_at
    redeemed   issued_at <= occurred_at < expires_at
    expired    occurred_at == expires_at
    every one  issued_at < expires_at

A record that fails any of it goes back ``ProcessingFailed`` with its data untouched, so Firehose
writes it to the bucket's ``processing-failed/`` prefix as it arrived, and the rest of the batch
loads. One bad record must not hold up a day's redemptions, and it must not be loaded either:
a malformed amount in a liability table is worse than a missing one, because the reconciliation
catches a missing one.

WHAT IT DERIVES
---------------

*   ``issue_month``        the first day of the issue month, the cohort
*   ``days_since_issue``   whole days from issue to this event: 0 for an issue, the exit day for a
                           redemption or an expiry -- the life table's time axis
*   ``days_to_redeem``     the same number for a redemption, null otherwise

All three come from the event's own ``issued_at``, never from an assumed term, and the
timestamps go out as ``YYYY-MM-DD HH:MM:SS`` in UTC, the form Redshift's ``TIMESTAMP`` reads.

It is deliberately stateless. Duplicates -- and the relay sends some -- pass straight through;
the warehouse drops them on ``event_id``, the one place that sees every copy.
"""

import base64
import json
import re
import uuid
from datetime import datetime, timezone

FIELDS = {"event_id", "voucher_code", "event_type", "amount_cents", "occurred_at", "issued_at",
          "expires_at"}
EVENT_TYPES = {"issued", "redeemed", "expired"}
CODE = re.compile(r"VCH-[0-9a-f]{8}")
STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
MAX_CENTS = 2 ** 63 - 1


class Malformed(ValueError):
    pass


def parse_time(value):
    if not isinstance(value, str) or not STAMP.fullmatch(value):
        raise Malformed("timestamp")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:  # 2026-02-30T...
        raise Malformed("timestamp") from exc


def transform(payload):
    """One stream record's bytes -> one output line, or ``Malformed``."""
    try:
        event = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        # RecursionError: a record nested deeper than the parser's stack, "[" * 100000. It is
        # this record's fault, and must not fail the batch it came in.
        raise Malformed("not JSON") from exc
    if not isinstance(event, dict) or set(event) != FIELDS:
        raise Malformed("fields")

    event_id = event["event_id"]
    try:
        if not isinstance(event_id, str) or str(uuid.UUID(event_id)) != event_id:
            raise Malformed("event_id")
    except ValueError as exc:
        raise Malformed("event_id") from exc
    if not isinstance(event["voucher_code"], str) or not CODE.fullmatch(event["voucher_code"]):
        raise Malformed("voucher_code")
    kind = event["event_type"]
    if not isinstance(kind, str) or kind not in EVENT_TYPES:
        raise Malformed("event_type")
    amount = event["amount_cents"]
    if type(amount) is not int or not 0 < amount <= MAX_CENTS:
        raise Malformed("amount_cents")

    occurred, issued, expires = (parse_time(event[k])
                                 for k in ("occurred_at", "issued_at", "expires_at"))
    in_order = issued < expires and {
        "issued": occurred == issued,
        "redeemed": issued <= occurred < expires,
        "expired": occurred == expires,
    }[kind]
    if not in_order:
        raise Malformed("timestamps out of order")

    days = int((occurred - issued).total_seconds() // 86400)
    row = {
        "event_id": event_id,
        "voucher_code": event["voucher_code"],
        "event_type": kind,
        "amount_cents": amount,
        "occurred_at": occurred.strftime("%Y-%m-%d %H:%M:%S"),
        "issued_at": issued.strftime("%Y-%m-%d %H:%M:%S"),
        "expires_at": expires.strftime("%Y-%m-%d %H:%M:%S"),
        "issue_month": issued.strftime("%Y-%m-01"),
        "days_since_issue": days,
        "days_to_redeem": days if kind == "redeemed" else None,
    }
    return (json.dumps(row, separators=(",", ":")) + "\n").encode("utf-8")


def lambda_handler(event, context):
    output, failed = [], 0
    for record in event["records"]:
        try:
            payload = base64.b64decode(record["data"], validate=True)
            line = transform(payload)
        except (ValueError, TypeError):  # Malformed and binascii.Error are both ValueErrors
            failed += 1
            output.append({"recordId": record["recordId"], "result": "ProcessingFailed",
                           "data": record["data"]})
            continue
        output.append({"recordId": record["recordId"], "result": "Ok",
                       "data": base64.b64encode(line).decode("ascii")})
    print(json.dumps({"records": len(output), "ok": len(output) - failed,
                      "processing_failed": failed}))
    return {"records": output}
