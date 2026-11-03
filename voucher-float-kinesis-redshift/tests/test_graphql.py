"""The GraphQL reads: finance only, three queries and no mutations, figures the warehouse agrees with.

These run on the same small chain as the warehouse tests: the service database the simulator
filled and the DuckDB warehouse built from what the relay, the transform and the COPY delivered.
The app is the real one with ``/graphql`` mounted, and the warehouse adapter is handed the open
DuckDB connection, because DuckDB will not open the same file twice in one process.
"""

import sqlite3

import pytest

from conftest import SECRET, USERS, bearer, body, fetch, login, warehouse
from voucher_service.app import create_app
from voucher_service.warehouse import DuckDBWarehouse


@pytest.fixture(scope="module")
def reads(chain):
    con, service_db, truth = chain
    app = create_app(service_db, jwt_secret=SECRET, users=USERS,
                     warehouse=DuckDBWarehouse(connection=con))
    client = app.test_client()
    return client, login(client, "finance"), con, service_db, truth


def ask(client, token, query, variables=None):
    response = client.post("/graphql", json={"query": query, "variables": variables or {}},
                           headers=bearer(token))
    return response.status_code, response.get_json()


def test_only_finance_may_read(reads):
    client, _, _, _, _ = reads
    query = {"query": "{ liability(asOf: \"2025-01-01\") { outstandingCents } }"}
    assert client.post("/graphql", json=query).status_code == 401
    for role in ("issuer", "merchant"):
        response = client.post("/graphql", json=query, headers=bearer(login(client, role)))
        assert response.status_code == 403 and body(response) == {"error": "forbidden"}


def test_there_are_exactly_three_queries_and_no_mutations(reads):
    client, token, _, _, _ = reads
    status, answer = ask(client, token, "{ __schema { queryType { fields { name } }"
                                        " mutationType { name } } }")
    assert status == 200
    fields = {f["name"] for f in answer["data"]["__schema"]["queryType"]["fields"]}
    assert fields == {"voucher", "liability", "cohort"}
    assert answer["data"]["__schema"]["mutationType"] is None


def test_a_voucher_comes_with_its_events_from_the_service_database(reads):
    client, token, _, service_db, _ = reads
    service = sqlite3.connect(service_db)
    code, amount, issued_at = service.execute(
        "select code, amount_cents, issued_at from vouchers where status = 'redeemed'"
        " order by issued_at limit 1").fetchone()
    service.close()
    status, answer = ask(client, token, """
        query ($code: String!) {
          voucher(code: $code) {
            code status amountCents issuedAt redeemedAt
            events { eventId eventType amountCents occurredAt publishedAt }
          }
        }""", {"code": code})
    voucher = answer["data"]["voucher"]
    assert status == 200 and voucher["code"] == code and voucher["status"] == "redeemed"
    assert voucher["amountCents"] == amount and voucher["issuedAt"] == issued_at
    assert [e["eventType"] for e in voucher["events"]] == ["issued", "redeemed"]
    assert voucher["events"][1]["occurredAt"] == voucher["redeemedAt"]
    assert all(e["publishedAt"] for e in voucher["events"])


@pytest.mark.parametrize("code", ["VCH-00000000", "not-a-code", "VCH-ABCDEF12"])
def test_an_unknown_voucher_is_null(reads, code):
    client, token, _, _, _ = reads
    status, answer = ask(client, token, "query ($c: String!) { voucher(code: $c) { code } }",
                         {"c": code})
    assert status == 200 and answer == {"data": {"voucher": None}}


def test_liability_is_the_reconciled_figure(reads):
    client, token, con, _, truth = reads
    final_day = fetch(con, "select max(day) as day from daily_liability")[0]["day"]
    status, answer = ask(client, token, "query ($d: Date!) { liability(asOf: $d) {"
                                        " asOf day outstandingCents outstandingVouchers } }",
                         {"d": final_day.isoformat()})
    check = warehouse.reconciliation(con)
    assert status == 200
    assert answer["data"]["liability"] == {
        "asOf": final_day.isoformat(), "day": final_day.isoformat(),
        "outstandingCents": check["service_cents"],
        "outstandingVouchers": check["service_vouchers"]}
    assert check["service_cents"] == truth["realised"]["open_cents_at_as_of"]


def test_liability_on_a_past_day_and_before_the_first(reads):
    client, token, con, _, _ = reads
    row = fetch(con, "select * from daily_liability where day = date '2024-12-20'")[0]
    query = "query ($d: Date!) { liability(asOf: $d) { day outstandingCents } }"
    assert ask(client, token, query, {"d": "2024-12-20"})[1]["data"]["liability"] == \
        {"day": "2024-12-20", "outstandingCents": row["outstanding_cents"]}
    assert ask(client, token, query, {"d": "2020-01-01"})[1]["data"]["liability"] == \
        {"day": None, "outstandingCents": 0}


def test_a_cohort_matches_the_warehouse_and_its_curve_climbs(reads):
    client, token, con, _, _ = reads
    row = fetch(con, "select * from breakage_by_issue_month"
                     " where issue_month = date '2024-11-01'")[0]
    status, answer = ask(client, token, """{
        cohort(month: "2024-11") {
          month issued redeemed expired open breakageRate
          d0: redeemedBy(days: 0) d1: redeemedBy(days: 1) d7: redeemedBy(days: 7)
          d14: redeemedBy(days: 14) d30: redeemedBy(days: 30) d90: redeemedBy(days: 90)
        } }""")
    cohort = answer["data"]["cohort"]
    assert status == 200
    assert (cohort["issued"], cohort["redeemed"], cohort["expired"], cohort["open"]) == \
        (row["issued_cents"], row["redeemed_cents"], row["expired_cents"], 0)
    assert cohort["breakageRate"] == pytest.approx(row["realised_breakage_rate"])
    curve = [cohort[k] for k in ("d0", "d1", "d7", "d14", "d30", "d90")]
    assert curve[0] == 0 and curve == sorted(curve)
    # A closed cohort's curve ends at what it redeemed, and that plus breakage is everything.
    assert cohort["d30"] == pytest.approx(row["redeemed_cents"] / row["issued_cents"])
    assert cohort["d90"] == cohort["d30"]
    assert cohort["d30"] + cohort["breakageRate"] == pytest.approx(1.0)


def test_an_open_cohort_is_projected(reads):
    client, token, con, _, _ = reads
    young = fetch(con, "select * from breakage_by_issue_month where open_vouchers > 0"
                       " order by issue_month desc limit 1")[0]
    month = young["issue_month"].strftime("%Y-%m")
    cohort = ask(client, token, "query ($m: String!) { cohort(month: $m) { open breakageRate } }",
                 {"m": month})[1]["data"]["cohort"]
    assert cohort["open"] == young["open_cents"] > 0
    assert cohort["breakageRate"] == pytest.approx(young["breakage_rate"])


@pytest.mark.parametrize("month", ["2024-13", "2024-1", "November", "2024-11-01"])
def test_a_malformed_month_is_refused(reads, month):
    client, token, _, _, _ = reads
    status, answer = ask(client, token, "query ($m: String!) { cohort(month: $m) { issued } }",
                         {"m": month})
    assert status == 200 and answer["data"] == {"cohort": None}
    assert answer["errors"] == [{"message": "invalid request", "path": ["cohort"]}]


def test_a_month_with_no_vouchers_is_null_and_bad_days_are_refused(reads):
    client, token, _, _, _ = reads
    assert ask(client, token, '{ cohort(month: "2019-01") { issued } }')[1] == \
        {"data": {"cohort": None}}
    answer = ask(client, token, '{ cohort(month: "2024-11") { redeemedBy(days: -1) } }')[1]
    assert answer["errors"][0]["message"] == "invalid request"


@pytest.mark.parametrize("payload", [
    None, [], {"query": 42}, {"query": "{ __typename }", "extra": 1},
    {"query": "{ __typename }", "variables": "not an object"},
])
def test_a_malformed_request_is_400(reads, payload):
    client, token, _, _, _ = reads
    response = client.post("/graphql", json=payload, headers=bearer(token))
    assert response.status_code == 400 and body(response) == {"error": "invalid request"}


def test_a_query_that_does_not_parse_or_validate_is_400(reads):
    client, token, _, _, _ = reads
    assert ask(client, token, "{ liability(asOf: ")[0] == 400
    status, answer = ask(client, token, "{ vouchers { code } }")
    assert status == 400 and answer["data"] is None and answer["errors"]


def test_a_failure_inside_a_resolver_says_only_internal_error(chain, tmp_path):
    _, service_db, _ = chain
    broken = DuckDBWarehouse(path=tmp_path / "no-such-warehouse.duckdb")
    client = create_app(service_db, jwt_secret=SECRET, users=USERS,
                        warehouse=broken).test_client()
    status, answer = ask(client, login(client, "finance"),
                         '{ liability(asOf: "2025-01-01") { outstandingCents } }')
    assert answer["errors"] == [{"message": "internal error", "path": ["liability"]}]
    assert "duckdb" not in str(answer).lower() and "no-such" not in str(answer)


def test_without_a_warehouse_the_voucher_query_still_works(chain):
    _, service_db, _ = chain
    client = create_app(service_db, jwt_secret=SECRET, users=USERS).test_client()
    token = login(client, "finance")
    answer = ask(client, token, '{ liability(asOf: "2025-01-01") { outstandingCents } }')[1]
    assert answer["errors"][0]["message"] == "warehouse unavailable"
    assert ask(client, token, '{ voucher(code: "VCH-00000000") { code } }')[1] == \
        {"data": {"voucher": None}}


def test_the_wsgi_entry_point_reads_its_settings_from_the_environment(chain):
    from voucher_service import wsgi
    con, service_db, _ = chain
    environ = {"VOUCHER_DB_PATH": str(service_db), "VOUCHER_JWT_SECRET": SECRET,
               "VOUCHER_FINANCE_PASSWORD": "rotated-finance-pass"}
    assert wsgi.warehouse_from(environ) is None
    assert isinstance(wsgi.warehouse_from({**environ, "VOUCHER_WAREHOUSE_PATH": "w.duckdb"}),
                      DuckDBWarehouse)
    app = wsgi.build(environ)
    assert app.config["JWT_SECRET"] == SECRET
    client = app.test_client()
    token = login(client, "finance", "rotated-finance-pass")
    assert ask(client, token, '{ voucher(code: "VCH-00000000") { code } }')[0] == 200
