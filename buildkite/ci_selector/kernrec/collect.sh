#!/usr/bin/env bash
# The recording build's last step: fold every job's kernel recordings into
# the per-step kernel table, pair it with the build's kernel symbol map, and
# publish both.
#
# Runs on a small CPU agent after every other step (the pipeline generator
# appends it when VLLM_CI_KERNREC=1). Needs nothing from the Buildkite API:
# the recordings arrive as this build's own artifacts, and each job's
# kernrec.json sidecar (written by ci_setup.sh on exit) names its step and
# exit status.
#
# Publishes to two places. Always: the table and the map as artifacts of this
# job. When the agent has AWS credentials and the bucket exists:
#
#   s3://$CI_SELECTOR_BUCKET/<pipeline>/<commit>/kernel_table.json.gz
#   s3://$CI_SELECTOR_BUCKET/<pipeline>/<commit>/kernel_symbol_map.json.gz
#   s3://$CI_SELECTOR_BUCKET/<pipeline>/latest.json        -> {commit, build, ...}
#
# Never fails the build for a publishing problem; the step is soft_fail and
# says on stderr what it could not do.
set -uo pipefail

BRANCH="${VLLM_CI_BRANCH:-main}"
RAW="https://raw.githubusercontent.com/vllm-project/ci-infra/${BRANCH}/buildkite/ci_selector/kernrec"
BUCKET="${CI_SELECTOR_BUCKET:-vllm-ci-selector}"
PIPELINE="${BUILDKITE_PIPELINE_SLUG:-ci}"
COMMIT="${BUILDKITE_COMMIT:?}"
BUILD="${BUILDKITE_BUILD_NUMBER:?}"

WORK="$(mktemp -d)"
cd "${WORK}" || exit 1
trap 'rm -rf -- "${WORK}"' EXIT

echo "--- :satellite: Collecting kernel recordings of build ${BUILD}"
buildkite-agent artifact download ".fnrec/**/*" . || echo "no kernel recordings in this build"
buildkite-agent artifact download "kernel_symbol_map.json.gz" . || echo "no kernel symbol map in this build"
n_files=$(find .fnrec -name 'kern.*.txt' 2>/dev/null | wc -l | tr -d ' ')
n_jobs=$(find .fnrec -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l | tr -d ' ')
echo "${n_files} recording files from ${n_jobs} jobs"
if [[ "${n_files}" == "0" ]]; then
  echo "nothing to fold; done"
  exit 0
fi

echo "--- :table_tennis_paddle_and_ball: Building the kernel table"
curl -sSfL --retry 3 -o kernel_table.py "${RAW}/kernel_table.py"
mkdir -p out
python3 kernel_table.py build --fnrec .fnrec --build "${BUILD}" --commit "${COMMIT}" \
  --pipeline "${PIPELINE}" --out out/kernel_table.json.gz
if [[ -f kernel_symbol_map.json.gz ]]; then
  mv kernel_symbol_map.json.gz out/
fi
python3 kernel_table.py show out/kernel_table.json.gz --top 10 || true

echo "--- :arrow_up: Uploading as build artifacts"
(cd out && buildkite-agent artifact upload "*")

echo "--- :s3: Publishing to s3://${BUCKET}/${PIPELINE}/${COMMIT}/"
if ! command -v aws >/dev/null 2>&1; then
  echo "aws cli not on this agent; the table and map are artifacts of this job only" >&2
  exit 0
fi
if ! aws sts get-caller-identity >/dev/null 2>&1; then
  echo "no AWS identity on this agent; the table and map are artifacts of this job only" >&2
  exit 0
fi
if aws s3 cp out/ "s3://${BUCKET}/${PIPELINE}/${COMMIT}/" --recursive --only-show-errors; then
  files=$(find out -maxdepth 1 -type f -exec basename {} \; | python3 -c 'import json,sys; print(json.dumps(sorted(sys.stdin.read().split())))')
  printf '{"commit":"%s","build":%s,"pipeline":"%s","published_at":"%s","files":%s}\n' \
    "${COMMIT}" "${BUILD}" "${PIPELINE}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${files}" > latest.json
  aws s3 cp latest.json "s3://${BUCKET}/${PIPELINE}/latest.json" --only-show-errors \
    && echo "published; latest.json -> ${COMMIT}"
else
  echo "S3 upload failed (bucket or write permission not in place?); artifacts are on this job" >&2
fi
