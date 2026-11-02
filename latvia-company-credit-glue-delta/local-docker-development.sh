#!/usr/bin/env bash
# Run the chain inside AWS's own Glue 5.0 image, on the committed sample.
#
#   ./local-docker-development.sh [stage|companies|proceedings|statements|model|test|all]
#
# The reference lab builds a Glue 3.0 image and hands spark-submit a delta-core 1.0.0 jar it
# downloaded itself. Glue 5.0 runs Delta Lake 3.3.0 natively, and its image already carries that
# jar at /usr/share/aws/delta/lib/, so there is nothing to download and nothing to pin by hand:
# the jobs run against the exact Spark (3.5.4-amzn-0), Delta, Python (3.11) and Java (17) the
# job you deploy runs on. The jar is on the image's classpath, but not on Python's path, so
# PYTHONPATH points at it too -- Python imports `delta.tables` straight out of a jar, which is a
# zip file -- and both spark-submit and pytest find the Delta Python API there.
#
# The default is `all`: the stages are a chain, not a menu. Each MERGE job reads what `stage`
# wrote and the model reads what the three MERGEs built.
#
# No ~/.aws mount and no AWS_PROFILE, unlike the lab: every stage reads the committed sample and
# writes into _localrun/docker/, so credentials in the container would buy nothing.
#
# --user: the image runs as `hadoop` (uid 10000), which cannot write into a checkout owned by the
# host user. Running as the host's uid and gid lets the jobs write their output where the host
# can read and delete it, and HOME=/tmp gives that unnamed user somewhere to keep its caches.
#
# The image's ENTRYPOINT is `bash -l`, so the container command is handed to a login shell, not
# exec'd: `docker run IMAGE python3 job.py` asks bash to run the python3 binary as a script and
# fails with "cannot execute binary file", while `spark-submit` survives only because it is a
# shell script. `-c "$*"` works for both. No argument below contains a space.
#
# DISABLE_SSL=true skips the self-signed keystore the image otherwise builds on every start.
# MSYS_NO_PATHCONV stops Git Bash rewriting the container path into a Windows one.
set -euo pipefail

IMAGE=public.ecr.aws/glue/aws-glue-libs:5
WORKSPACE=/workspace
DELTA_JAR=/usr/share/aws/delta/lib/delta-spark.jar
ROOT=_localrun/docker/bucket

glue() {
  MSYS_NO_PATHCONV=1 docker run --rm \
    --user "$(id -u):$(id -g)" -e HOME=/tmp \
    -v "$(pwd):${WORKSPACE}" -w "${WORKSPACE}" \
    -e DISABLE_SSL=true -e PYTHONPATH="${DELTA_JAR}" \
    "${IMAGE}" -c "$*"
}

submit() {
  glue spark-submit --py-files "${DELTA_JAR}" "$@"
}

stage=${1:-all}
case "${stage}" in
  stage)
    mkdir -p "${ROOT}"
    # One batch a year, cut on 30 September: the model reads the same rows as from quarterly
    # batches (the notebook shows the two runs agree row for row) in a quarter of the commits.
    glue python3 local-development/stage_batches.py --local --data sample --clean \
      --cadence year --root "${ROOT}"
    ;;
  companies|proceedings|statements)
    submit glue-jobs/glue-delta-merge.py --local --root "${ROOT}" --table "${stage}"
    ;;
  model)
    submit glue-jobs/glue-credit-model.py --local --root "${ROOT}"
    ;;
  test)
    glue python3 -m pytest tests -q -p no:cacheprovider
    ;;
  all)
    for s in stage companies proceedings statements model; do bash "$0" "$s"; done
    ;;
  *)
    echo "usage: $0 [stage|companies|proceedings|statements|model|test|all]" >&2
    exit 2
    ;;
esac
