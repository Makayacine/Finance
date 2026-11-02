"""Run the whole chain on a laptop: stage the batches, MERGE the three tables, fit and score.

    python local-development/run_chain.py --data sample --cadence year
    python local-development/run_chain.py --data raw

Each step is the same command the README gives on its own, run as its own process in order, so a
failure stops the chain at the step that failed with everything before it in place. Every step
writes into ``_localrun/bucket/``; ``--clean`` (the default) empties it first, and the step
timings are kept in ``results/chain.json``.
"""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data", choices=("sample", "raw"), default="sample")
    parser.add_argument("--root", default=str(PROJECT / "_localrun" / "bucket"))
    parser.add_argument("--cadence", choices=("quarter", "year"), default="quarter")
    parser.add_argument("--keep", action="store_true", help="do not empty the bucket first")
    args = parser.parse_args()

    python = sys.executable
    stage = [python, "local-development/stage_batches.py", "--local", "--data", args.data,
             "--root", args.root, "--cadence", args.cadence] + ([] if args.keep else ["--clean"])
    steps = [("stage", stage)]
    for table in ("companies", "proceedings", "statements"):
        steps.append((table, [python, "glue-jobs/glue-delta-merge.py", "--local", "--root",
                              args.root, "--table", table]))
    steps.append(("model", [python, "glue-jobs/glue-credit-model.py", "--local", "--root",
                            args.root]))

    started, seconds = time.monotonic(), {}
    for name, command in steps:
        begun = time.monotonic()
        result = subprocess.run(command, cwd=PROJECT)
        if result.returncode:
            sys.exit(f"{name} failed with exit code {result.returncode}; the bucket is left as "
                     f"it stands, and a re-run of that step carries on from it")
        seconds[name] = round(time.monotonic() - begun)
        print(f"== {name}: {seconds[name]} s", flush=True)
    seconds["chain"] = round(time.monotonic() - started)
    print(f"== chain: {seconds['chain']} s", flush=True)
    record = Path(args.root) / "results" / "chain.json"
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps({"data": args.data, "cadence": args.cadence,
                                  "seconds": seconds}, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
