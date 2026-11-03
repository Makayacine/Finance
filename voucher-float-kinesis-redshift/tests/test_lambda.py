"""The Firehose transform, fed Firehose's own event shape.

``firehose-lambda/sample-events/firehose-batch.json`` is a transformation event as Firehose sends
it -- ``invocationId``, the stream and delivery ARNs, and records carrying ``recordId``,
``approximateArrivalTimestamp``, base64 ``data`` and the Kinesis metadata. It holds four good
records, one of them a relay retry, and seven bad ones, the lab's CSV line among them. The same
file is the console test event for the function you deploy.
"""

import base64
import json

import pytest

from conftest import PROJECT
from conftest import transform as transform_module

lambda_handler, transform, Malformed = (transform_module.lambda_handler,
                                        transform_module.transform, transform_module.Malformed)
SAMPLE = json.loads((PROJECT / "firehose-lambda" / "sample-events" / "firehose-batch.json")
                    .read_text())
OUTPUT_COLUMNS = ["event_id", "voucher_code", "event_type", "amount_cents", "occurred_at",
                  "issued_at", "expires_at", "issue_month", "days_since_issue", "days_to_redeem"]


def decoded(record):
    return json.loads(base64.b64decode(record["data"]))


def event(**overrides):
    base = {"event_id": "3f0c6c1e-8a4b-4d43-9a37-5b1f8f0e2a11", "voucher_code": "VCH-5e1a9c07",
            "event_type": "issued", "amount_cents": 25000,
            "occurred_at": "2026-10-05T09:14:22Z", "issued_at": "2026-10-05T09:14:22Z",
            "expires_at": "2026-11-04T09:14:22Z"}
    base.update(overrides)
    return json.dumps(base).encode("utf-8")


def test_every_record_comes_back_once_in_order(capsys):
    result = lambda_handler(SAMPLE, None)
    assert [r["recordId"] for r in result["records"]] == \
        [r["recordId"] for r in SAMPLE["records"]]
    assert [r["result"] for r in result["records"]] == ["Ok"] * 4 + ["ProcessingFailed"] * 7
    assert json.loads(capsys.readouterr().out) == {"records": 11, "ok": 4,
                                                   "processing_failed": 7}


def test_good_records_become_json_lines_with_the_derived_fields():
    issued, redeemed, expired, retry = lambda_handler(SAMPLE, None)["records"][:4]
    lines = [base64.b64decode(r["data"]).decode() for r in (issued, redeemed, expired)]
    assert all(line.endswith("\n") and line.count("\n") == 1 for line in lines)
    assert decoded(issued) == {
        "event_id": "3f0c6c1e-8a4b-4d43-9a37-5b1f8f0e2a11", "voucher_code": "VCH-5e1a9c07",
        "event_type": "issued", "amount_cents": 25000, "occurred_at": "2026-10-05 09:14:22",
        "issued_at": "2026-10-05 09:14:22", "expires_at": "2026-11-04 09:14:22",
        "issue_month": "2026-10-01", "days_since_issue": 0, "days_to_redeem": None}
    assert decoded(redeemed)["days_to_redeem"] == decoded(redeemed)["days_since_issue"] == 3
    assert decoded(expired)["days_since_issue"] == 30
    assert decoded(expired)["days_to_redeem"] is None
    assert decoded(expired)["issue_month"] == "2026-10-01"
    assert retry["data"] == redeemed["data"]  # stateless: the warehouse drops the copy
    assert list(decoded(issued)) == OUTPUT_COLUMNS


def test_bad_records_go_back_untouched():
    for original, result in list(zip(SAMPLE["records"], lambda_handler(SAMPLE, None)["records"]))[4:]:
        assert result["result"] == "ProcessingFailed"
        assert result["data"] == original["data"]


def test_the_issue_month_and_the_age_come_from_the_event_not_from_a_30_day_term():
    longer = event(event_type="expired", occurred_at="2026-12-04T09:14:22Z",
                   expires_at="2026-12-04T09:14:22Z")  # a 60-day voucher
    row = json.loads(transform(longer))
    assert row["days_since_issue"] == 60 and row["issue_month"] == "2026-10-01"


def test_days_are_whole_days_rounded_down():
    almost = event(event_type="redeemed", occurred_at="2026-11-04T09:14:21Z")
    assert json.loads(transform(almost))["days_to_redeem"] == 29
    same_day = event(event_type="redeemed", occurred_at="2026-10-05T23:59:59Z")
    assert json.loads(transform(same_day))["days_to_redeem"] == 0


def test_one_record_too_deep_to_parse_fails_alone():
    deep = base64.b64encode(b"[" * 100_000).decode()
    good = base64.b64encode(event()).decode()
    answer = lambda_handler({"records": [{"recordId": "deep", "data": deep},
                                         {"recordId": "good", "data": good}]}, None)
    assert [(r["recordId"], r["result"]) for r in answer["records"]] == \
        [("deep", "ProcessingFailed"), ("good", "Ok")]
    assert answer["records"][0]["data"] == deep


@pytest.mark.parametrize("payload", [
    event(amount_cents=True),
    event(amount_cents=0),
    event(amount_cents=-1),
    event(amount_cents=2 ** 63),
    event(amount_cents="25000"),
    event(event_id="3F0C6C1E-8A4B-4D43-9A37-5B1F8F0E2A11"),
    event(event_id="not-a-uuid"),
    event(voucher_code="VCH-5E1A9C07"),
    event(voucher_code="CODE-5e1a9c07"),
    event(event_type=["issued"]),
    event(occurred_at="2026-10-05 09:14:22", issued_at="2026-10-05 09:14:22"),
    event(occurred_at="2026-02-30T09:14:22Z", issued_at="2026-02-30T09:14:22Z"),
    event(occurred_at="2026-10-05T09:14:23Z"),                         # issued after issue
    event(event_type="expired", occurred_at="2026-11-03T09:14:22Z"),  # expired before expiry
    event(event_type="redeemed", occurred_at="2026-10-05T09:14:21Z"),  # redeemed before issue
    event(expires_at="2026-10-05T09:14:22Z"),                         # expires at issue
    b'{"event_id": NaN}',
    b"\xff\xfe not utf-8",
    b"[]",
], ids=["bool", "zero", "negative", "past-int64", "string-amount", "uppercase-uuid", "bad-uuid",
        "uppercase-code", "foreign-code", "list-type", "space-stamp", "feb-30", "issue-drift",
        "early-expiry", "redeemed-before-issue", "zero-term", "nan", "not-utf8", "array"])
def test_malformed_events_are_refused(payload):
    with pytest.raises(Malformed):
        transform(payload)
    record = {"recordId": "r1", "data": base64.b64encode(payload).decode()}
    [result] = lambda_handler({"records": [record]}, None)["records"]
    assert result == {"recordId": "r1", "result": "ProcessingFailed", "data": record["data"]}
