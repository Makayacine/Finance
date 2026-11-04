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
XLSX = b"PK\x03\x04 a workbook"
PDF = b"%PDF-1.4 a schedule"
OECD = b"STRUCTURE,REF_AREA,MEASURE,TIME_PERIOD,OBS_VALUE\nDATAFLOW,ZAF,IRLT,2026-09,8.947\n"
IMF = b"observation_date,INTGSBZAM193N\n2026-09-01,8.97\n"
LISTING = (b'<a href="../">up</a><a href="Schedule of domestic government bonds as at 30 September 2026.pdf">x</a>'
           b'<a href="Schedule of domestic government bonds as at 31 Janaury 2023.pdf">x</a>'
           b'<a href="Schedule of domestic government bonds as at 31 October 2026.pdf">x</a>')


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

    def test_nyfed_asks_for_the_day_or_a_range_from_start(self):
        opener = ScriptedOpener(b'{"refRates": []}', b'{"refRates": []}')
        fetch.fetch_nyfed(DAY, opener)
        fetch.fetch_nyfed(DAY, opener, start=dt.date(2008, 9, 2))
        self.assertIn("startDate=2026-10-30&endDate=2026-10-30", opener.requests[0])
        self.assertIn("startDate=2008-09-02&endDate=2026-10-30", opener.requests[1])


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
        "nt_auctions": make("nt_auctions", XLSX),
        "nt_schedule": make("nt_schedule", PDF),
        "oecd": make("oecd", OECD),
        "imf": make("imf", IMF),
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
            "raw/imf/date=2026-10-30/INTGSBZAM193N.csv",
            "raw/nt_auctions/date=2026-10-30/fixed-rate-bonds-2026-27.xlsx",
            "raw/nt_schedule/date=2026-10-30/schedule.pdf",
            "raw/nyfed/date=2026-10-30/rates.json",
            "raw/oecd/date=2026-10-30/irlt.csv",
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


class SouthAfricaTest(unittest.TestCase):
    def test_the_auction_file_is_the_fiscal_years(self):
        self.assertEqual(fetch.nt_fiscal_year(dt.date(2027, 3, 31)), "2026-27")
        self.assertEqual(fetch.nt_auctions_name(DAY), "fixed-rate-bonds-2026-27.xlsx")
        self.assertEqual(fetch.nt_auctions_name(dt.date(2019, 5, 1)), "fixed-rate-bonds-2019-20.xls")
        opener = ScriptedOpener(XLSX)
        self.assertEqual(fetch.fetch_nt_auctions(DAY, opener), XLSX)
        self.assertEqual(opener.requests[0], fetch.NT_AUCTIONS_URL + "Fixed-rate%20bond%20auctions%20-%202026-27.xlsx")

    def test_a_page_in_place_of_a_spreadsheet_is_refused(self):
        with self.assertRaises(fetch.FetchError):
            fetch.fetch_nt_auctions(DAY, ScriptedOpener(b"<html>not found</html>"))

    def test_the_schedule_is_the_newest_dated_on_or_before_the_day(self):
        listed = fetch.nt_schedules_listed(ScriptedOpener(LISTING))
        self.assertEqual(sorted(listed), [dt.date(2023, 1, 31), dt.date(2026, 9, 30), dt.date(2026, 10, 31)])
        opener = ScriptedOpener(LISTING, PDF)
        self.assertEqual(fetch.fetch_nt_schedule(DAY, opener), PDF)
        self.assertTrue(opener.requests[1].endswith("as%20at%2030%20September%202026.pdf"))
        with self.assertRaises(fetch.FetchError):
            fetch.fetch_nt_schedule(DAY, ScriptedOpener(LISTING, b"<html></html>"))

    def test_a_reset_connection_is_tried_again_but_a_404_is_not(self):
        reset = fetch.urllib.error.URLError(ConnectionResetError(104, "Connection reset by peer"))
        waits = []
        opener = ScriptedOpener(reset, reset, XLSX)
        self.assertEqual(fetch._nt_get("u", opener, sleep=waits.append), XLSX)
        self.assertEqual(waits, [2.0, 4.0])
        missing = fetch.urllib.error.HTTPError("u", 404, "Not Found", {}, None)
        with self.assertRaises(fetch.urllib.error.HTTPError):
            fetch._nt_get("u", ScriptedOpener(missing, XLSX), sleep=waits.append)
        with self.assertRaises(fetch.urllib.error.URLError):
            fetch._nt_get("u", ScriptedOpener(reset, reset, reset), sleep=waits.append)

    def test_the_history_takes_every_fiscal_years_file_with_a_pause_between(self):
        listing = (b'<a href="Fixed-rate bond auctions - 2010-13.xls">a</a><a href="Fixed-rate bond auctions - 2025-26.xlsx">b</a>'
                   b'<a href="../">up</a>')
        pauses = []
        out = fetch.fetch_nt_auction_history(ScriptedOpener(listing, XLSX, XLSX), pause_seconds=2.0, sleep=pauses.append)
        self.assertEqual(sorted(out), ["fixed-rate-bonds-2010-13.xls", "fixed-rate-bonds-2025-26.xlsx"])
        self.assertEqual(pauses, [2.0])

    def test_the_two_series_are_checked_for_their_own_headers(self):
        self.assertEqual(fetch.fetch_oecd(DAY, ScriptedOpener(OECD)), OECD)
        self.assertEqual(fetch.fetch_imf(DAY, ScriptedOpener(IMF)), IMF)
        with self.assertRaises(fetch.FetchError):
            fetch.fetch_oecd(DAY, ScriptedOpener(b"<html>"))
        with self.assertRaises(fetch.FetchError):
            fetch.fetch_imf(DAY, ScriptedOpener(b"DATE,X\n"))


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
        self.assertEqual(len(keys), 7)
        self.assertEqual(self.s3.get_object(Bucket="curves-raw", Key=keys[1])["Body"].read(), PAGE)
        run = self.glue.get_job_run(JobName="curves-load", RunId=result["glue_job_run_id"])["JobRun"]
        self.assertEqual(run["Arguments"]["--date_filter"], "2026-10-30")
        self.assertEqual(run["Arguments"]["--load_type"], "incremental")
        self.assertEqual(run["Arguments"]["--sources"], "fedinvest,gsw,auctions,nt_auctions,nt_schedule,oecd,imf")
        self.assertTrue(result["sources"]["nyfed"].startswith("failed"))

    def test_nothing_arrived_means_no_load_and_a_failed_invocation(self):
        with self.assertRaises(fetch.FetchError):
            self.run_handler(fail=tuple(fetch.SOURCES))
        self.assertEqual(self.glue.get_job_runs(JobName="curves-load")["JobRuns"], [])

    def test_default_date_is_today_in_utc(self):
        frozen = dt.datetime(2026, 10, 30, 23, 30, tzinfo=dt.timezone.utc)
        with mock.patch.object(fetch.dt, "datetime", wraps=dt.datetime) as clock:
            clock.now.return_value = frozen
            self.assertEqual(fetch._price_date({}), DAY)
            clock.now.assert_called_with(dt.timezone.utc)


if __name__ == "__main__":
    unittest.main()
