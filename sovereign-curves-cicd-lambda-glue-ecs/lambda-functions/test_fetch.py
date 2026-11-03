"""The fetcher, against stand-in web servers and moto's S3 and Glue.

No test reaches the network: each source's HTTP is answered by a scripted opener that records
what it was sent, so the tests can check the CSRF handshake itself, not only its result.
"""

import datetime as dt
import json
import os
import unittest
from unittest import mock

import boto3
from moto import mock_aws

import fetch

DAY = dt.date(2026, 10, 30)
FORM = b'<form><input type="hidden" name="_csrf" value="tok-123" /></form>'
PAGE = b"<h2>Prices For: October 30, 2026</h2><table class=\"data1\"></table>"
GSW = b'"Note"\n\nDate,BETA0,BETA1\n2026-10-23,1,2\n'


class Response:
    def __init__(self, body):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class ScriptedOpener:
    """Answers each request from a list, and keeps every request it was given."""

    def __init__(self, *bodies):
        self.bodies = list(bodies)
        self.requests = []

    def open(self, req, timeout=None):
        self.requests.append(req)
        body = self.bodies.pop(0)
        if isinstance(body, Exception):
            raise body
        return Response(body)


class FedInvestTest(unittest.TestCase):
    def test_get_for_the_token_then_post_it_with_the_date(self):
        opener = ScriptedOpener(FORM, PAGE)
        self.assertEqual(fetch.fetch_fedinvest(DAY, opener), PAGE)
        get, post = opener.requests
        self.assertEqual(get, fetch.FEDINVEST_URL)
        self.assertEqual(post.get_method(), "POST")
        body = dict(pair.split("=") for pair in post.data.decode().split("&"))
        self.assertEqual(body["priceDate"], "2026-10-30")
        self.assertEqual(body["_csrf"], "tok-123")

    def test_a_form_without_a_token_stops_before_the_post(self):
        opener = ScriptedOpener(b"<form></form>")
        with self.assertRaises(fetch.FetchError):
            fetch.fetch_fedinvest(DAY, opener)
        self.assertEqual(len(opener.requests), 1)

    def test_one_session_keeps_the_cookie(self):
        opener = fetch._opener()
        processors = [h for h in opener.handlers if h.__class__.__name__ == "HTTPCookieProcessor"]
        self.assertEqual(len(processors), 1)


class SourceChecksTest(unittest.TestCase):
    def test_gsw_without_its_header_is_refused(self):
        with self.assertRaises(fetch.FetchError):
            fetch.fetch_gsw(DAY, ScriptedOpener(b"<html>maintenance</html>"))
        self.assertEqual(fetch.fetch_gsw(DAY, ScriptedOpener(GSW)), GSW)

    def test_auctions_asks_for_the_last_fourteen_days(self):
        opener = ScriptedOpener(json.dumps({"data": []}).encode())
        fetch.fetch_auctions(DAY, opener)
        url = opener.requests[0]
        self.assertIn("auction_date:gte:2026-10-16,auction_date:lte:2026-10-30", url)

    def test_nyfed_must_be_json_with_rates(self):
        with self.assertRaises(KeyError):
            fetch.fetch_nyfed(DAY, ScriptedOpener(b'{"error": "x"}'))


def scripted_sources(fail=()):
    def make(name, body):
        def fn(day):
            if name in fail:
                raise TimeoutError(f"{name} timed out")
            return body
        return (fn, fetch.SOURCES[name][1])
    return {
        "fedinvest": make("fedinvest", PAGE),
        "gsw": make("gsw", GSW),
        "auctions": make("auctions", b'{"data": []}'),
        "nyfed": make("nyfed", b'{"refRates": []}'),
    }


class IsolationTest(unittest.TestCase):
    def test_raw_keys_are_posix_on_every_os(self):
        self.assertEqual(fetch.raw_key("fedinvest", DAY, "prices.html"), "raw/fedinvest/date=2026-10-30/prices.html")

    def test_one_failure_does_not_stop_the_others(self):
        written = {}
        status = fetch.fetch_all(DAY, written.__setitem__, fetchers=scripted_sources(fail=("gsw",)))
        self.assertTrue(status["gsw"].startswith("failed: TimeoutError"))
        self.assertEqual(sorted(written), [
            "raw/auctions/date=2026-10-30/auctions.json",
            "raw/fedinvest/date=2026-10-30/prices.html",
            "raw/nyfed/date=2026-10-30/rates.json",
        ])

    def test_a_failed_write_is_isolated_too(self):
        def put(key, body):
            if "nyfed" in key:
                raise PermissionError("AccessDenied")
        status = fetch.fetch_all(DAY, put, fetchers=scripted_sources())
        self.assertTrue(status["nyfed"].startswith("failed: PermissionError"))
        self.assertTrue(status["fedinvest"].startswith("ok "))

    def test_backfill_pauses_between_dates(self):
        pauses = []
        days = [DAY - dt.timedelta(days=i) for i in range(3)]
        out = fetch.backfill(days, lambda k, b: None, pause_seconds=2.0, fetchers=scripted_sources(), sleep=pauses.append)
        self.assertEqual(pauses, [2.0, 2.0])
        self.assertEqual(list(out), days)
        self.assertEqual(list(out[DAY]), ["fedinvest"])


@mock_aws
class HandlerTest(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"RAW_BUCKET": "curves-raw", "GLUE_JOB_NAME": "curves-load",
                                                "AWS_DEFAULT_REGION": "us-east-1"})
        self.env.start()
        self.s3 = boto3.client("s3", region_name="us-east-1")
        self.s3.create_bucket(Bucket="curves-raw")
        self.glue = boto3.client("glue", region_name="us-east-1")
        self.glue.create_job(Name="curves-load", Role="arn:aws:iam::123456789012:role/glue",
                             Command={"Name": "pythonshell", "PythonVersion": "3.9", "ScriptLocation": "s3://b/load.py"})

    def tearDown(self):
        self.env.stop()

    def run_handler(self, fail=()):
        with mock.patch.dict(fetch.SOURCES, scripted_sources(fail=fail)):
            return fetch.lambda_handler({"date_filter": "2026-10-30"}, None, s3=self.s3, glue=self.glue)

    def test_writes_each_source_and_starts_the_load_for_that_date(self):
        result = self.run_handler(fail=("nyfed",))
        keys = sorted(o["Key"] for o in self.s3.list_objects_v2(Bucket="curves-raw")["Contents"])
        self.assertEqual(len(keys), 3)
        self.assertEqual(self.s3.get_object(Bucket="curves-raw", Key=keys[1])["Body"].read(), PAGE)
        run = self.glue.get_job_run(JobName="curves-load", RunId=result["glue_job_run_id"])["JobRun"]
        self.assertEqual(run["Arguments"]["--date_filter"], "2026-10-30")
        self.assertEqual(run["Arguments"]["--load_type"], "incremental")
        self.assertEqual(run["Arguments"]["--sources"], "fedinvest,gsw,auctions")
        self.assertTrue(result["sources"]["nyfed"].startswith("failed"))

    def test_nothing_arrived_means_no_load_and_a_failed_invocation(self):
        with self.assertRaises(fetch.FetchError):
            self.run_handler(fail=("fedinvest", "gsw", "auctions", "nyfed"))
        self.assertEqual(self.glue.get_job_runs(JobName="curves-load")["JobRuns"], [])

    def test_default_date_is_today_in_utc(self):
        frozen = dt.datetime(2026, 10, 30, 23, 30, tzinfo=dt.timezone.utc)
        with mock.patch.object(fetch.dt, "datetime", wraps=dt.datetime) as clock:
            clock.now.return_value = frozen
            self.assertEqual(fetch._price_date({}), DAY)
            clock.now.assert_called_with(dt.timezone.utc)


if __name__ == "__main__":
    unittest.main()
