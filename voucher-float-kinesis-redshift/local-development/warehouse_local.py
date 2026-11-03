"""The Redshift side on a laptop: COPY the bucket into DuckDB, build the views, reconcile.

    python local-development/warehouse_local.py --as-of 2026-11-02T00:00:00Z

    _localrun/bucket/voucher-events/**/*.jsonl   ->  voucher_events_raw          (the COPY)
    warehouse/landing-tables.sql                 ->  the three tables, as in redshift/
    warehouse/views.sql                          ->  the six views, the text Redshift runs
    _localrun/voucher-service.db                 ->  service_control_total       (the other side)
                                                 ->  _localrun/warehouse.duckdb

Each object is copied once: the loader keeps the keys it has copied in ``_copied_objects``, the
bookkeeping Firehose does for itself on AWS. Copying an object twice would not change a single
figure -- ``voucher_event`` drops copies on ``event_id`` -- but ``duplicates_dropped`` in the
reconciliation would then count the loader's mistakes along with the relay's retries.

The control total is read straight from the service database, in one query with the moment the
service last changed -- its latest issue, redemption or sweep. The service can only be read as it
is now, so the total describes that moment, and it is stamped with the warehouse's as_of only
when as_of is no earlier: an as_of before the service's latest change is refused, rather than
put on a balance from later. Without ``--as-of`` the warehouse reports as of that change. Even
then the reconciliation is only meaningful when both sides describe the same moment: the outbox
drained, the stream delivered and the views reading every event up to as_of.
"""

import argparse
import contextlib
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import duckdb

PROJECT = Path(__file__).resolve().parents[1]
LOCAL = PROJECT / "_localrun"
LANDING_SQL = PROJECT / "warehouse" / "landing-tables.sql"
VIEWS_SQL = PROJECT / "warehouse" / "views.sql"


def landing_columns(table="voucher_events_raw"):
    """(name, type) for each column of ``table`` in landing-tables.sql -- the one column list."""
    text = LANDING_SQL.read_text(encoding="utf-8")
    block = re.search(rf"create table if not exists {table} \((.*?)\n\);", text, re.S).group(1)
    return [(m.group(1), m.group(2).upper())
            for m in re.finditer(r"^\s+(\w+)\s+([a-z]+)\b", block, re.M)]


def connect(path=LOCAL / "warehouse.duckdb"):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(path))


def create(con):
    con.execute(LANDING_SQL.read_text(encoding="utf-8"))
    con.execute("create table if not exists _copied_objects (object_key varchar primary key)")
    con.execute(VIEWS_SQL.read_text(encoding="utf-8"))


def quoted(text):
    return "'" + str(text).replace("'", "''") + "'"


def copy_new(con, bucket_dir=LOCAL / "bucket"):
    """COPY every object under ``voucher-events/`` not copied before; returns rows landed."""
    root = Path(bucket_dir)
    keys = sorted(p.relative_to(root).as_posix()
                  for p in (root / "voucher-events").rglob("*.jsonl"))
    done = {row[0] for row in con.execute("select object_key from _copied_objects").fetchall()}
    new = [key for key in keys if key not in done]
    if not new:
        return 0
    columns = landing_columns()
    names = ", ".join(name for name, _ in columns)
    struct = ", ".join(f"{quoted(name)}: {quoted(kind)}" for name, kind in columns)
    files = ", ".join(quoted((root / key).as_posix()) for key in new)
    before = con.execute("select count(*) from voucher_events_raw").fetchone()[0]
    con.execute("begin")
    con.execute(f"insert into voucher_events_raw ({names}) select {names} from read_json("
                f"[{files}], format = 'newline_delimited', columns = {{{struct}}},"
                f" timestampformat = '%Y-%m-%d %H:%M:%S', dateformat = '%Y-%m-%d')")
    con.executemany("insert into _copied_objects values (?)", [(key,) for key in new])
    con.execute("commit")
    return con.execute("select count(*) from voucher_events_raw").fetchone()[0] - before


def set_as_of(con, as_of):
    if isinstance(as_of, str):
        as_of = datetime.strptime(as_of, "%Y-%m-%dT%H:%M:%SZ")
    as_of = as_of.astimezone(timezone.utc).replace(tzinfo=None) if as_of.tzinfo else as_of
    con.execute("delete from warehouse_as_of")
    con.execute("insert into warehouse_as_of values (?)", [as_of])


class ServiceMovedOn(ValueError):
    """The service has changed since the moment the warehouse is asked to report as of."""


def read_service(service_db):
    """(unredeemed vouchers, their cents, the service's latest change), in one read."""
    with contextlib.closing(sqlite3.connect(service_db)) as service:
        vouchers, cents, latest = service.execute(
            "select count(case when status = 'unredeemed' then 1 end),"
            " coalesce(sum(case when status = 'unredeemed' then amount_cents end), 0),"
            " max(max(issued_at), coalesce(max(redeemed_at), ''), coalesce(max(swept_at), ''))"
            " from vouchers").fetchone()
    if latest:
        latest = datetime.strptime(latest, "%Y-%m-%dT%H:%M:%SZ")
    return vouchers, cents, latest


def take_control_total(con, service_db):
    """The service's own count and sum of unredeemed vouchers, as the other side of the check,
    stamped with the warehouse's as_of -- or ServiceMovedOn if the service has changed since."""
    vouchers, cents, latest = read_service(service_db)
    as_of = con.execute("select as_of from warehouse_as_of").fetchone()[0]
    if latest and latest > as_of:
        raise ServiceMovedOn(
            f"the service last changed at {latest:%Y-%m-%dT%H:%M:%SZ}, after as_of"
            f" {as_of:%Y-%m-%dT%H:%M:%SZ}: its total now is not its total then")
    con.execute("delete from service_control_total")
    con.execute("insert into service_control_total values (?, ?, ?)", [as_of, vouchers, cents])


def reconciliation(con):
    cursor = con.execute("select * from reconciliation")
    names = [d[0] for d in cursor.description]
    return dict(zip(names, cursor.fetchone()))


def build(service_db=LOCAL / "voucher-service.db", bucket_dir=LOCAL / "bucket",
          warehouse=LOCAL / "warehouse.duckdb", as_of=None):
    fresh = not Path(warehouse).exists()
    con = connect(warehouse)
    create(con)
    landed = copy_new(con, bucket_dir)
    if as_of is None:
        as_of = read_service(service_db)[2] or \
            con.execute("select max(occurred_at) from voucher_events_raw").fetchone()[0]
    con.execute("begin")  # as_of and the total it is checked against move together, or not at all
    try:
        set_as_of(con, as_of)
        take_control_total(con, service_db)
    except BaseException:
        con.execute("rollback")
        con.close()
        if fresh:  # a refused first build would leave events with no as_of, reading as zero
            for leftover in (Path(warehouse), Path(f"{warehouse}.wal")):
                leftover.unlink(missing_ok=True)
        raise
    con.execute("commit")
    return con, landed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--service-db", default=str(LOCAL / "voucher-service.db"))
    parser.add_argument("--bucket-dir", default=str(LOCAL / "bucket"))
    parser.add_argument("--warehouse", default=str(LOCAL / "warehouse.duckdb"))
    parser.add_argument("--as-of", help="UTC, as 2026-11-02T00:00:00Z, no earlier than the"
                        " service's latest change; default: that change")
    args = parser.parse_args(argv)
    try:
        con, landed = build(args.service_db, args.bucket_dir, args.warehouse, args.as_of)
    except ServiceMovedOn as refused:
        print(f"refused: {refused}", file=sys.stderr)
        return 2
    check = reconciliation(con)
    con.close()
    print(f"copied {landed:,} row(s); reconciliation: {check}")
    return 0 if check["difference_cents"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
