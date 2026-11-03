"""Run the service or the sweep from the command line.

    python -m voucher_service serve --db _localrun/voucher-service.db --port 5000 \
        --warehouse _localrun/warehouse.duckdb
    python -m voucher_service sweep --db _localrun/voucher-service.db [--now 2026-12-01T00:00:00Z]

``serve`` is Flask's own server, bound to localhost: enough to walk the API with curl. The one you
deploy runs ``voucher_service.app:create_app`` under a WSGI server such as gunicorn.
"""

import argparse
import contextlib
import sys
from pathlib import Path

from . import db
from .app import create_app
from .sweep import sweep
from .timeutil import from_text, utc_now
from .warehouse import DuckDBWarehouse


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m voucher_service")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="run the API on Flask's development server")
    serve.add_argument("--db", required=True)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=5000)
    serve.add_argument("--warehouse", help="the DuckDB file GraphQL's liability and cohort read")
    expire = commands.add_parser("sweep", help="expire every overdue unredeemed voucher")
    expire.add_argument("--db", required=True)
    expire.add_argument("--now", help="UTC, as 2026-12-01T00:00:00Z; defaults to the wall clock")
    args = parser.parse_args(argv)

    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    if args.command == "serve":
        store = DuckDBWarehouse(args.warehouse) if args.warehouse else None
        create_app(args.db, warehouse=store).run(host=args.host, port=args.port, threaded=True)
        return 0
    db.create_schema(args.db)
    moment = from_text(args.now) if args.now else utc_now()
    with contextlib.closing(db.connect(args.db)) as conn:
        expired = sweep(conn, moment)
    print(f"expired {expired} voucher(s) as of {args.now or 'now'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
