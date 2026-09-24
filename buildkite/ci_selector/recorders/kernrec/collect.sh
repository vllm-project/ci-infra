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
# job, whatever state they are in, for debugging. Only when both validate
# (table has rows, map has objects and no failure reason) and the agent has
# AWS credentials:
#
#   s3://$CI_SELECTOR_BUCKET/<pipeline>/<commit>/kernel_table.json.gz
#   s3://$CI_SELECTOR_BUCKET/<pipeline>/<commit>/kernel_symbol_map.json.gz
#   s3://$CI_SELECTOR_BUCKET/<pipeline>/latest.json        -> {commit, build, ...}
#
# Nothing reaches S3 unless the pair is complete. Several builds can collect
# the same commit (daily and nightly often share one), and a later run with
# a bad map must not overwrite the good one that latest.json already points
# at.
#
# Never fails the build for a publishing problem; the step is soft_fail and
# says on stderr what it could not do.
set -uo pipefail

BRANCH="${VLLM_CI_BRANCH:-main}"
RAW="https://raw.githubusercontent.com/vllm-project/ci-infra/${BRANCH}/buildkite/ci_selector/recorders/kernrec"
BUCKET="${CI_SELECTOR_BUCKET:-vllm-ci-selector}"
PIPELINE="${BUILDKITE_PIPELINE_SLUG:-ci}"
COMMIT="${BUILDKITE_COMMIT:?}"
BUILD="${BUILDKITE_BUILD_NUMBER:?}"
# Fold another build of the same pipeline instead of this one, for when a
# recording build's own collect step is stuck behind a job that never gets a
# machine. The source must be at this build's commit.
FROM=()
if [[ -n "${KERNREC_SOURCE_BUILD_ID:-}" ]]; then
  FROM=(--build "${KERNREC_SOURCE_BUILD_ID}")
  BUILD="${KERNREC_SOURCE_BUILD_NUMBER:?set with KERNREC_SOURCE_BUILD_ID}"
  echo "folding build ${BUILD} (${KERNREC_SOURCE_BUILD_ID}) from build ${BUILDKITE_BUILD_NUMBER}"
fi

WORK="$(mktemp -d)"
cd "${WORK}" || exit 1
trap 'rm -rf -- "${WORK}"' EXIT

echo "--- :satellite: Collecting kernel recordings of build ${BUILD}"
if [[ -n "${KERNREC_SOURCE_JOBS:-}" ]]; then
  # One search per job: a build with the Python recorder on holds tens of
  # thousands of artifacts, and one search over all of them times out.
  printf '%s\n' ${KERNREC_SOURCE_JOBS} | xargs -P 8 -I{} sh -c \
    'buildkite-agent artifact download ".kernrec/{}/*" . "$@" >/dev/null 2>&1 || echo "no recordings for job {}"' _ ${FROM[@]+"${FROM[@]}"}
else
  buildkite-agent artifact download ".kernrec/**/*" . ${FROM[@]+"${FROM[@]}"} || echo "no kernel recordings in this build"
fi
buildkite-agent artifact download "kernel_symbol_map.json.gz" . ${FROM[@]+"${FROM[@]}"} || echo "no kernel symbol map in this build"
n_files=$(find .kernrec -name 'kern.*.txt' 2>/dev/null | wc -l | tr -d ' ')
n_jobs=$(find .kernrec -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l | tr -d ' ')
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
python3 kernel_table.py build --kernrec .kernrec --build "${BUILD}" --commit "${COMMIT}" \
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

# Both gates come before anything touches S3. The commit prefix is written
# as a unit: a rerun of this commit with a broken half must leave the good
# pair (and latest.json, which may already point here) exactly as it was.
if [[ "${table_ok}" != "yes" ]]; then
  echo "table has no rows; not publishing" >&2
  exit 1
fi
if [[ "${map_ok}" != "yes" ]]; then
  echo "no usable symbol map in this build; not publishing (${COMMIT}/ keeps whatever an earlier build put there)" >&2
  exit 1
fi

# Only main's recordings describe the tree PRs are selected against, and only
# postmerge agents can write the bucket anyway. A branch under test stops here,
# green, with everything on this job's artifacts.
if [[ "${BUILDKITE_BRANCH:-main}" != "main" ]]; then
  echo "branch ${BUILDKITE_BRANCH} is not main; not publishing (the artifacts above are the result)"
  exit 0
fi

echo "--- :s3: Publishing to s3://${BUCKET}/${PIPELINE}/kernrec/${COMMIT}/"
if ! command -v aws >/dev/null 2>&1; then
  echo "aws cli not on this agent; the table and map are artifacts of this job only" >&2
  exit 0
fi
if ! aws sts get-caller-identity >/dev/null 2>&1; then
  echo "no AWS identity on this agent; the table and map are artifacts of this job only" >&2
  exit 0
fi
if ! aws s3 cp out/ "s3://${BUCKET}/${PIPELINE}/kernrec/${COMMIT}/" --recursive --only-show-errors; then
  echo "S3 upload failed (bucket or write permission not in place?); artifacts are on this job" >&2
  exit 1
fi
files=$(find out -maxdepth 1 -type f -exec basename {} \; | python3 -c 'import json,sys; print(json.dumps(sorted(sys.stdin.read().split())))')
printf '{"commit":"%s","build":%s,"pipeline":"%s","published_at":"%s","files":%s}\n' \
  "${COMMIT}" "${BUILD}" "${PIPELINE}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${files}" > latest.json
aws s3 cp latest.json "s3://${BUCKET}/${PIPELINE}/kernrec/latest.json" --only-show-errors \
  && echo "published; latest.json -> ${COMMIT}"
