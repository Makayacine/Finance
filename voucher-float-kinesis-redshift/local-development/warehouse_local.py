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

The control total is read straight from the service database, as one query, at the moment the
warehouse reports as of. The reconciliation is only meaningful when both sides describe the same
moment: the outbox drained, the stream delivered and the views reading every event up to as_of.
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


def take_control_total(con, service_db, taken_at):
    """The service's own count and sum of unredeemed vouchers, as the other side of the check."""
    with contextlib.closing(sqlite3.connect(service_db)) as service:
        vouchers, cents = service.execute(
            "select count(*), coalesce(sum(amount_cents), 0) from vouchers"
            " where status = 'unredeemed'").fetchone()
    con.execute("delete from service_control_total")
    con.execute("insert into service_control_total values (?, ?, ?)",
                [con.execute("select as_of from warehouse_as_of").fetchone()[0]
                 if taken_at is None else taken_at, vouchers, cents])


def reconciliation(con):
    cursor = con.execute("select * from reconciliation")
    names = [d[0] for d in cursor.description]
    return dict(zip(names, cursor.fetchone()))


def build(service_db=LOCAL / "voucher-service.db", bucket_dir=LOCAL / "bucket",
          warehouse=LOCAL / "warehouse.duckdb", as_of=None):
    con = connect(warehouse)
    create(con)
    landed = copy_new(con, bucket_dir)
    if as_of is None:
        as_of = con.execute("select max(occurred_at) from voucher_events_raw").fetchone()[0]
    set_as_of(con, as_of)
    take_control_total(con, service_db, None)
    return con, landed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--service-db", default=str(LOCAL / "voucher-service.db"))
    parser.add_argument("--bucket-dir", default=str(LOCAL / "bucket"))
    parser.add_argument("--warehouse", default=str(LOCAL / "warehouse.duckdb"))
    parser.add_argument("--as-of", help="UTC, as 2026-11-02T00:00:00Z; default: the last event")
    args = parser.parse_args(argv)
    con, landed = build(args.service_db, args.bucket_dir, args.warehouse, args.as_of)
    check = reconciliation(con)
    con.close()
    print(f"copied {landed:,} row(s); reconciliation: {check}")
    return 0 if check["difference_cents"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
