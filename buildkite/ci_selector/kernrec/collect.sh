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
# From here on a failure is a failure: soft_fail keeps the build green, but
# a half-built result must never be published as if it were complete.
curl -sSfL --retry 3 -o kernel_table.py "${RAW}/kernel_table.py" || { echo "cannot fetch kernel_table.py" >&2; exit 1; }
mkdir -p out
python3 kernel_table.py build --fnrec .fnrec --build "${BUILD}" --commit "${COMMIT}" \
  --pipeline "${PIPELINE}" --out out/kernel_table.json.gz || { echo "table build failed" >&2; exit 1; }
python3 kernel_table.py show out/kernel_table.json.gz --top 10 || true

# Validate the pair before anything leaves this job.
table_ok=$(python3 - <<'PY'
import gzip, json
try:
    t = json.load(gzip.open("out/kernel_table.json.gz", "rt"))
    print("yes" if t.get("rows") else "no")
except Exception:
    print("no")
PY
)
map_ok=no
if [[ -f kernel_symbol_map.json.gz ]]; then
  map_ok=$(python3 - <<'PY'
import gzip, json
try:
    m = json.load(gzip.open("kernel_symbol_map.json.gz", "rt"))
    print("yes" if m.get("objects") and not m.get("reason") else "no")
except Exception:
    print("no")
PY
)
  mv kernel_symbol_map.json.gz out/
fi
echo "table usable: ${table_ok}; symbol map usable: ${map_ok}"

echo "--- :arrow_up: Uploading as build artifacts"
(cd out && buildkite-agent artifact upload "*")

if [[ "${table_ok}" != "yes" ]]; then
  echo "table has no rows; not publishing" >&2
  exit 1
fi

echo "--- :s3: Publishing to s3://${BUCKET}/${PIPELINE}/${COMMIT}/"
if ! command -v aws >/dev/null 2>&1; then
  echo "aws cli not on this agent; the table and map are artifacts of this job only" >&2
  exit 0
fi
if ! aws sts get-caller-identity >/dev/null 2>&1; then
  echo "no AWS identity on this agent; the table and map are artifacts of this job only" >&2
  exit 0
fi
if ! aws s3 cp out/ "s3://${BUCKET}/${PIPELINE}/${COMMIT}/" --recursive --only-show-errors; then
  echo "S3 upload failed (bucket or write permission not in place?); artifacts are on this job" >&2
  exit 1
fi
# latest.json only ever points at a complete pair: a consumer following it
# must find both files, and a map with no objects is no map.
if [[ "${map_ok}" != "yes" ]]; then
  echo "no usable symbol map in this build; ${COMMIT}/ has the table but latest.json is unchanged" >&2
  exit 1
fi
files=$(find out -maxdepth 1 -type f -exec basename {} \; | python3 -c 'import json,sys; print(json.dumps(sorted(sys.stdin.read().split())))')
printf '{"commit":"%s","build":%s,"pipeline":"%s","published_at":"%s","files":%s}\n' \
  "${COMMIT}" "${BUILD}" "${PIPELINE}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${files}" > latest.json
aws s3 cp latest.json "s3://${BUCKET}/${PIPELINE}/latest.json" --only-show-errors \
  && echo "published; latest.json -> ${COMMIT}"
