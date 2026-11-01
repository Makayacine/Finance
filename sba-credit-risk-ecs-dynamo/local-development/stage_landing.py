"""Land a batch: copy every entity's source files into ``new/<entity>/``, then write the marker.

    data/raw/  or  data/sample/            ->   <root>/new/<entity>/<file>
    data/sba_recovery_rates.csv (always)   ->   <root>/new/sba_recovery_rates/
    and last of all                        ->   <root>/new/_READY

This is the host's half of the pipeline: the upload. The entity list in ``credit_risk/config.py``
decides which file goes to which folder, so this script names no entity itself.

**The marker is written last, and it is the only object the EventBridge rule matches.** The
reference lab fires on every arrival under ``new/``, which here would start eight executions for
one batch, seven of them failing the gate because their siblings had not landed yet. With a
failed gate now a red Fail state rather than a quiet Pass, that would be seven false alarms per
batch. One marker per batch makes one execution per batch, and makes a red one mean something.

    python local-development/stage_landing.py --local --data sample --clean
    python local-development/stage_landing.py --local --data raw --clean
    python local-development/stage_landing.py --bucket <landing-bucket> --data raw
"""

import argparse
import shutil
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from credit_risk.config import ENTITIES, READY_MARKER  # noqa: E402

DATA_ROOTS = {"raw": PROJECT / "data" / "raw", "sample": PROJECT / "data" / "sample"}


def batch(data):
    """``[(local path, key)]`` for every file of every entity, in entity order."""
    pairs = []
    for entity in ENTITIES:
        root = PROJECT / "data" if entity.shared_source else DATA_ROOTS[data]
        files = sorted(root.glob(entity.source_glob))
        if len(files) != entity.expected_files:
            raise FileNotFoundError(
                f"{entity.name}: {len(files)} files match {root / entity.source_glob}, "
                f"expected {entity.expected_files}")
        pairs += [(path, f"{entity.landing}/{path.name}") for path in files]
    return pairs


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--local", action="store_true")
    parser.add_argument("--root", default=str(Path("_localrun") / "bucket"))
    parser.add_argument("--bucket")
    parser.add_argument("--data", choices=sorted(DATA_ROOTS), default="sample")
    parser.add_argument("--clean", action="store_true", help="empty the local bucket first")
    args = parser.parse_args(argv)

    pairs = batch(args.data)
    if args.local:
        root = Path(args.root)
        if args.clean and root.exists():
            shutil.rmtree(root)
        for path, key in pairs:
            target = root.joinpath(*key.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
            print(f"{key}  <-  {path.relative_to(PROJECT)}")
        root.joinpath(*READY_MARKER.split("/")).write_text("", encoding="utf-8")
    else:
        import boto3

        client = boto3.client("s3")
        for path, key in pairs:
            client.upload_file(str(path), args.bucket, key)
            print(f"s3://{args.bucket}/{key}")
        client.put_object(Bucket=args.bucket, Key=READY_MARKER, Body=b"")
    print(f"{len(pairs)} files landed, then {READY_MARKER}")


if __name__ == "__main__":
    main()
