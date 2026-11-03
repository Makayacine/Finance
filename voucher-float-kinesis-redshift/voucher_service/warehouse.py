"""Where the GraphQL reads that need the warehouse go: DuckDB locally, Redshift on AWS.

    DuckDBWarehouse          the file ``local-development/warehouse_local.py`` builds
    RedshiftDataWarehouse    the Redshift Data API -- no driver, no open connection, IAM auth

Both run the same three statements against the views in ``warehouse/views.sql``. The statements
are written once, with ``:name`` parameters, which is the Data API's form; DuckDB takes them as
``$name``. boto3 is imported inside the Redshift class only, so the local service and the tests
never import it.
"""

import re
import time
from datetime import date

LIABILITY = """
select day, outstanding_cents, outstanding_vouchers
from daily_liability
where day <= cast(:as_of as date)
order by day desc
limit 1
"""

COHORT = """
select issue_month, issued_cents, redeemed_cents, expired_cents, open_cents, breakage_rate
from breakage_by_issue_month
where issue_month = cast(:month as date)
"""

REDEEMED_BY = """
select redeemed_share
from redemption_life_table
where scope = 'cohort' and issue_month = cast(:month as date)
  and day <= cast(:last_day as integer)
order by day desc
limit 1
"""


class Warehouse:
    def query(self, sql, params):
        raise NotImplementedError

    def liability(self, as_of):
        rows = self.query(LIABILITY, {"as_of": as_of.isoformat()})
        if not rows:
            return {"day": None, "outstanding_cents": 0, "outstanding_vouchers": 0}
        day, cents, vouchers = rows[0]
        return {"day": _as_date(day), "outstanding_cents": int(cents),
                "outstanding_vouchers": int(vouchers)}

    def cohort(self, month):
        rows = self.query(COHORT, {"month": month.isoformat()})
        if not rows:
            return None
        _, issued, redeemed, expired, still_open, rate = rows[0]
        return {"issued": int(issued), "redeemed": int(redeemed), "expired": int(expired),
                "open": int(still_open), "breakage_rate": float(rate)}

    def redeemed_by(self, month, days):
        """The cohort's share of issued value redeemed within ``days`` days of issue."""
        if days <= 0:
            return 0.0
        rows = self.query(REDEEMED_BY, {"month": month.isoformat(), "last_day": str(days - 1)})
        return float(rows[0][0]) if rows else 0.0


def _as_date(value):
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


class DuckDBWarehouse(Warehouse):
    """Give it the file, or a connection already open on it (DuckDB will not open one file twice
    in one process with different settings). Each query runs on its own cursor."""

    def __init__(self, path=None, connection=None):
        self.path, self.connection = path, connection

    def query(self, sql, params):
        sql = re.sub(r":(\w+)", r"$\1", sql)
        if self.connection is not None:
            return self.connection.cursor().execute(sql, params).fetchall()
        import duckdb
        with duckdb.connect(str(self.path), read_only=True) as con:
            return con.execute(sql, params).fetchall()


class RedshiftDataWarehouse(Warehouse):
    """``workgroup`` for Redshift Serverless, or ``cluster`` and ``db_user`` for a provisioned
    cluster; the caller's IAM role needs ``redshift-data:ExecuteStatement``,
    ``DescribeStatement`` and ``GetStatementResult``, and ``redshift-serverless:GetCredentials``
    or ``redshift:GetClusterCredentials``."""

    def __init__(self, database, workgroup=None, cluster=None, db_user=None, region=None,
                 schema="voucher_float", client=None, poll_seconds=0.25, timeout_seconds=30):
        if client is None:
            import boto3
            client = boto3.client("redshift-data", region_name=region)
        self.client = client
        self.target = {"Database": database}
        if workgroup:
            self.target["WorkgroupName"] = workgroup
        else:
            self.target.update(ClusterIdentifier=cluster, DbUser=db_user)
        self.schema = schema
        self.poll_seconds, self.timeout_seconds = poll_seconds, timeout_seconds

    def query(self, sql, params):
        # A Data API call is its own session, with no search_path: name the schema.
        sql = re.sub(r"\bfrom (\w+)", rf"from {self.schema}.\1", sql)
        statement = self.client.execute_statement(
            Sql=sql, Parameters=[{"name": k, "value": v} for k, v in params.items()],
            **self.target)
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            status = self.client.describe_statement(Id=statement["Id"])
            if status["Status"] == "FINISHED":
                break
            if status["Status"] in ("FAILED", "ABORTED") or time.monotonic() > deadline:
                raise RuntimeError(f"warehouse query {status['Status'].lower()}")
            time.sleep(self.poll_seconds)
        if not status.get("HasResultSet"):
            return []
        result = self.client.get_statement_result(Id=statement["Id"])
        return [[None if cell.get("isNull") else next(iter(cell.values())) for cell in record]
                for record in result["Records"]]
