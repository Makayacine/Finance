"""Run the six-state chain once, in order, stopping at the first state that fails.

The Azure counterpart of ``step-functions/step-functions.json`` in the AWS sibling: the same six
states, the same names, the same order, the same arguments to the jobs that take them. It is a
sequential Python runner rather than a hosted orchestrator because the chain has no branching, no
retries and no parallelism to orchestrate -- and the one property that matters is the one a plain
loop gives for free:

    ExtractMailOffers  ->  RawIngestMailOffers  ->  ExtractClientAttributes
      ->  RawIngestClientAttributes  ->  ProcessedLayer  ->  BanditRefineryPath3

WHY A CHAIN AND NOT A FAN-OUT, CARRIED OVER UNCHANGED
-----------------------------------------------------
Every table lands on one fixed blob name that each run overwrites, so a table's warehouse load must
finish before its next extract starts. The AWS definition is a chain for that reason and so is
this. Parallelising the two tables would be safe in principle -- they land on different blobs --
and is not done, because the processed layer needs both loaded and the saving is seconds.

EACH STATE IS ITS OWN PROCESS
-----------------------------
A Step Functions task runs a Glue job: a fresh process with its own connection, its own exit code
and its own log. Each state here is a subprocess for the same reasons. A job that leaks a
transaction, a temp table or a global cannot hand it to the next job; a job that exits non-zero --
the extractor's ``sys.exit(1)`` when the chain and the config entity disagree -- stops the chain
exactly where a failed Glue task stops the state machine; and the jobs are exercised through their
command lines, which is how anything else will run them.

Connection strings reach the jobs through the environment the runner inherits (see
``python-jobs/azure_common.py``). The runner adds only the per-run locations: the source stand-in,
the container, the config table and the database.

    python orchestration/run-chain.py --source-db _localrun/source.duckdb
"""

import argparse
import logging
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
JOBS = os.path.join(PROJECT, "python-jobs")
sys.path.insert(0, JOBS)

import azure_common as az  # noqa: E402  (the path above is what makes it importable)

LOG = logging.getLogger("run_chain")

EXTRACT = "mysql-extraction.py"
RAW_INGEST = "azure-sql-raw-ingestion.py"
PROCESSED = "azure-sql-processed-layer.py"
REFINERY = "refinery-path3.py"

# (state name, job file, arguments), in chain order. Names and arguments are the Step Functions
# definition's; tests/test_warehouse.py reads that JSON and asserts these agree with it. The two
# arguments the AWS raw-ingestion task also passes, --bucket and --iam-role, have no counterpart:
# the container has a default scoped to the storage account, and SQL Server reads nothing from
# storage on its own behalf, so there is no role for it to assume.
STATES = [
    ("ExtractMailOffers", EXTRACT, ["--table_name", "mail_offers", "--load_type", "incremental"]),
    ("RawIngestMailOffers", RAW_INGEST, ["--table_name", "mail_offers"]),
    ("ExtractClientAttributes", EXTRACT,
     ["--table_name", "client_attributes", "--load_type", "full_load"]),
    ("RawIngestClientAttributes", RAW_INGEST, ["--table_name", "client_attributes"]),
    ("ProcessedLayer", PROCESSED, []),
    ("BanditRefineryPath3", REFINERY, []),
]


def arguments_for(job, args):
    """The per-run locations each job takes, on top of the state's own arguments."""
    if job == EXTRACT:
        extra = ["--container", args.container, "--config-table", args.config_table]
        return extra + (["--source-db", args.source_db] if args.source_db else [])
    if job == RAW_INGEST:
        return ["--container", args.container, "--database", args.database]
    extra = ["--database", args.database]
    if job == REFINERY:
        extra += ["--draws", str(args.draws), "--bootstrap-replicates", str(args.replicates)]
    return extra


def run_chain(args):
    """Run every state in order. Returns 0, or the exit code of the state that failed."""
    started = time.time()
    for name, job, state_arguments in STATES:
        command = [sys.executable, os.path.join(JOBS, job)] + state_arguments \
            + arguments_for(job, args)
        LOG.info("state %s: %s", name, " ".join([job] + state_arguments))
        began = time.time()
        code = subprocess.call(command)
        if code != 0:
            LOG.error("state %s failed with exit code %s after %.1fs; the chain stops here, as "
                      "a failed task stops the state machine", name, code, time.time() - began)
            return code
        LOG.info("state %s succeeded in %.1fs", name, time.time() - began)
    LOG.info("chain complete: %s states in %.1fs", len(STATES), time.time() - started)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source-db", default=None,
                        help="the DuckDB stand-in for MySQL, passed to both extract states. "
                             "Without it they read MySQL from the MYSQL_* environment")
    parser.add_argument("--container", default=az.DEFAULT_CONTAINER,
                        help="landing-zone container (default: %(default)s)")
    parser.add_argument("--config-table", default=az.CONFIG_TABLE,
                        help="Table Storage table holding the watermark (default: %(default)s)")
    parser.add_argument("--database", default=az.DEFAULT_DATABASE,
                        help="SQL database (default: %(default)s)")
    parser.add_argument("--draws", type=int, default=az.load_aws_job(
                            "glue-refinery-path3.py", "aws_glue_refinery_path3").DEFAULT_DRAWS,
                        help="the refinery's Thompson draws (default: %(default)s)")
    parser.add_argument("--replicates", type=int, default=az.load_aws_job(
                            "glue-refinery-path3.py", "aws_glue_refinery_path3").DEFAULT_REPLICATES,
                        help="the refinery's bootstrap replicates (default: %(default)s)")
    args = parser.parse_args()
    sys.exit(run_chain(args))


if __name__ == "__main__":
    main()
