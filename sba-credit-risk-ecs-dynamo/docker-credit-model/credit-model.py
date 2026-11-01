"""ECS Fargate task 2: fit both credit chains from the ready folders and write the results.

    <entity>/*  ->  SBA scorecard, hazard, severity, open-book EL, IFRS 9 stage, IRB capital
                ->  FDIC PD, insurer loss rate, run-off table, EL backtest, counterparty EL
                ->  results/<chain>/*.csv, DynamoDB items, then archived/<entity>/*

The state machine runs this with ``ecs:runTask.sync``: it returns nothing the next state needs,
so the ECS task's own exit is the whole answer, and a non-zero exit fails the state. The batch
only moves to ``archived/`` after every item is written; see ``credit_risk/pipeline.py``.

    python credit-model.py --local                     # folders for S3, a JSON file for DynamoDB
    BUCKET=<bucket> TABLE=sba_credit_risk_results python credit-model.py
"""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from credit_risk.config import RESULTS_TABLE  # noqa: E402
from credit_risk.pipeline import run_models  # noqa: E402
from credit_risk.stores import DynamoTable, FolderStore, JsonTable, S3Store  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--local", action="store_true")
    parser.add_argument("--root", default=os.path.join("_localrun", "bucket"))
    parser.add_argument("--table-file", default=os.path.join("_localrun", "dynamodb",
                                                             f"{RESULTS_TABLE}.json"))
    parser.add_argument("--bootstrap", type=int, default=500)
    args = parser.parse_args(argv)

    if args.local:
        store, table = FolderStore(args.root), JsonTable(args.table_file)
    else:
        store = S3Store(os.environ["BUCKET"])
        table = DynamoTable(os.environ.get("TABLE", RESULTS_TABLE))
    summary = run_models(store, table, n_boot=args.bootstrap)
    print(json.dumps(summary, indent=2, default=str))
    return summary


if __name__ == "__main__":
    main()
