"""The whole chain on a laptop, in order, from an empty ``_localrun/``.

    python local-development/run_local.py              # about 50,000 vouchers, about 2 minutes
    python local-development/run_local.py --scale 0.1  # a tenth of the volume

    simulate.py         two years of vouchers through the REST endpoints  -> voucher-service.db
    outbox-relay.py     --local: the outbox into the stream folder         -> stream/
    firehose_local.py   the transform Lambda, batch by batch               -> bucket/
    warehouse_local.py  the COPY, the views, the service's control total   -> warehouse.duckdb

It stops with exit code 1 unless the warehouse's outstanding liability equals the service's own
sum of unredeemed cents. A full-scale run rewrites ``data/simulation-truth.json``, byte for byte
the committed one; a scaled run writes its truth into ``_localrun/`` instead.
"""

import argparse
import shutil
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(PROJECT))

import firehose_local  # noqa: E402
import simulate  # noqa: E402
import warehouse_local  # noqa: E402

LOCAL = PROJECT / "_localrun"


def load_relay():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "outbox_relay", PROJECT / "kinesis-producer" / "outbox-relay.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(scale=1.0):
    if LOCAL.exists():
        shutil.rmtree(LOCAL)
    LOCAL.mkdir()
    truth_path = simulate.TRUTH if scale == 1.0 else LOCAL / "simulation-truth.json"
    timings = {}

    started = time.monotonic()
    truth = simulate.simulate(LOCAL / "voucher-service.db", simulate.Parameters(scale=scale),
                              truth_path)
    timings["simulate"] = time.monotonic() - started

    started = time.monotonic()
    relay = load_relay()
    conn = sqlite3.connect(LOCAL / "voucher-service.db")
    conn.row_factory = sqlite3.Row
    relayed = relay.relay(conn, relay.FolderStream(LOCAL / "stream" / "voucher-events"))
    conn.close()
    timings["relay"] = time.monotonic() - started

    started = time.monotonic()
    delivered = firehose_local.deliver()
    timings["firehose"] = time.monotonic() - started

    started = time.monotonic()
    con, landed = warehouse_local.build(as_of=truth["as_of"])
    check = warehouse_local.reconciliation(con)
    pooled = con.execute("select max(expired_share) from redemption_life_table"
                         " where scope = 'pooled'").fetchone()[0]
    latest = con.execute("select issue_month, open_vouchers, naive_breakage_rate, breakage_rate"
                         " from breakage_by_issue_month where open_vouchers > 0"
                         " order by issued_vouchers desc limit 1").fetchone()
    con.close()
    timings["warehouse"] = time.monotonic() - started

    r = truth["realised"]
    print(f"simulated   {r['issued']:,} vouchers, {r['issued_cents'] / 100:,.2f} issued, as of"
          f" {truth['as_of']}; {r['redeemed']:,} redeemed, {r['expired']:,} expired,"
          f" {r['open_at_as_of']:,} open")
    print(f"relayed     {relayed.published:,} events in {relayed.calls:,} put_records calls")
    print(f"delivered   {delivered['records']:,} records in {delivered['batches']} batches:"
          f" {delivered['ok']:,} Ok, {delivered['processing_failed']:,} ProcessingFailed")
    print(f"landed      {landed:,} rows in voucher_events_raw")
    print(f"reconciled  service {check['service_cents'] / 100:,.2f} on"
          f" {check['service_vouchers']:,} vouchers, warehouse"
          f" {check['warehouse_cents'] / 100:,.2f} on {check['warehouse_vouchers']:,};"
          f" difference {check['difference_cents']} cents; {check['duplicates_dropped']}"
          f" duplicates dropped, {check['orphan_exits']} orphan exits,"
          f" {check['vouchers_with_two_exits']} vouchers with two exits")
    print(f"breakage    pooled life table {pooled:.4f} against a true {truth['true_breakage_rate']:.4f};"
          f" the open {latest[0]:%Y-%m} cohort, {latest[1]:,} vouchers still open:"
          f" {latest[3]:.4f} projected, {latest[2]:.4f} naive")
    print("seconds     " + ", ".join(f"{k} {v:.1f}" for k, v in timings.items()))
    return check["difference_cents"] == 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--scale", type=float, default=1.0)
    args = parser.parse_args(argv)
    return 0 if run(args.scale) else 1


if __name__ == "__main__":
    sys.exit(main())
