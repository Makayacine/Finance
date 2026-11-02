"""ECS Fargate task 1: check every landed file against its entity's dtypes, promote, and gate.

    new/<entity>/*   ->   <entity>/*   for every file whose columns and dtypes match
                     ->   {"valid_files": ..., "expected_files": ..., "gate_passed": ...}

The state machine starts this task with ``.waitForTaskToken`` and not ``.sync``, because
``ecs:runTask.sync`` hands the next state the ECS task description -- ARNs, exit codes, timings
-- and nothing the container computed. The validator's answer is the counts, so the task token
arrives as the ``TASK_TOKEN`` environment variable and the answer goes back through
``SendTaskSuccess``. An exception raised while importing the validation code or running it goes
back through ``SendTaskFailure`` with the traceback as its cause, and fails the state at once. A
process that dies without running that handler -- an out-of-memory kill, an image that never
starts -- sends nothing, and the state fails only when its ``TimeoutSeconds`` run out.

A gate that does not pass is not an error here. The validator reports it; the state machine's
Choice turns it into a Fail state, which is where a short batch is meant to go red.

    python data-validity.py --local                         # folders for S3, prints the answer
    python data-validity.py --local --output answer.json    # and writes it, for the local runner
    BUCKET=<bucket> TASK_TOKEN=<token> python data-validity.py
"""

import argparse
import json
import os
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from credit_risk.stores import FolderStore, S3Store, report_local, report_task  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--local", action="store_true")
    parser.add_argument("--root", default=os.path.join("_localrun", "bucket"))
    parser.add_argument("--output")
    args = parser.parse_args(argv)

    if args.local:
        from credit_risk.validation import run_validation

        output = run_validation(FolderStore(args.root))
        if args.output:
            Path(args.output).write_text(json.dumps(output, indent=2), encoding="utf-8")
        return report_local(output)

    token = os.environ["TASK_TOKEN"]
    try:
        # Imported here, not at the top: the validation code pulls in pandas, and an image whose
        # pandas will not import must still answer its token rather than leave the state waiting.
        from credit_risk.validation import run_validation

        output = run_validation(S3Store(os.environ["BUCKET"]))
    except Exception:
        report_task(token, error=traceback.format_exc())
        raise
    report_task(token, output=output)
    return output


if __name__ == "__main__":
    main()
