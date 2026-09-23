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
#   s3://$CI_SELECTOR_BUCKET/<pipeline>/<commit>/table.json.gz   (Python record,
#                                        when the build recorded one and it built)
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

echo "--- :snake: Building the Python coverage table"
# Optional: the kernel pair publishes with or without it. Needs the
# ci_selector package (uv, Python 3.12, the recorders' minor) and this build's
# vLLM checkout, which the agent made for this step. Everything else comes off
# the artifacts: job identity and exit status from each kernrec.json, test
# counts from the pytest plugin (ci_selector/scripts/artifacts.py says how).
py_ok=no
n_fn=$(find .fnrec -name 'fn.*.txt' 2>/dev/null | wc -l | tr -d ' ')
CHECKOUT="${BUILDKITE_BUILD_CHECKOUT_PATH:-}"
if [[ "${n_fn}" == "0" ]]; then
  echo "no Python recordings in this build (VLLM_CI_FNREC unset?)"
elif [[ -z "${CHECKOUT}" ]] || ! git -C "${CHECKOUT}" cat-file -e "${COMMIT}^{commit}" 2>/dev/null; then
  echo "no vLLM checkout holding ${COMMIT}; skipping the Python table" >&2
else
  if ! command -v uv >/dev/null 2>&1; then
    curl -LsSf --retry 3 https://astral.sh/uv/install.sh \
      | env UV_INSTALL_DIR="${WORK}/bin" UV_NO_MODIFY_PATH=1 sh >/dev/null 2>&1
    export PATH="${WORK}/bin:${PATH}"
  fi
  if command -v uv >/dev/null 2>&1 \
     && git clone -q --depth 1 --branch "${BRANCH}" https://github.com/vllm-project/ci-infra "${WORK}/ci-infra"; then
    SEL="${WORK}/ci-infra/buildkite/ci_selector"
    run_sel() { uv run -q --no-dev --python 3.12 --project "${SEL}" "$@"; }
    if run_sel ci-sweep-from-artifacts .fnrec --out "sweep/${PIPELINE}-${BUILD}" \
       && run_sel ci-build-table "${CHECKOUT}" "sweep/${PIPELINE}-${BUILD}" -o out/table.json.gz; then
      py_ok=yes
    else
      echo "Python table build failed; the kernel record publishes without it" >&2
      rm -f out/table.json.gz
    fi
  else
    echo "no uv or no ci-infra checkout of ${BRANCH}; skipping the Python table" >&2
  fi
fi
echo "${n_fn} Python recording files; Python table built: ${py_ok}"

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
files=$(find out -maxdepth 1 -type f -exec basename {} \; | python3 -c 'import json,sys; print(json.dumps(sorted(sys.stdin.read().split())))')
printf '{"commit":"%s","build":%s,"pipeline":"%s","published_at":"%s","files":%s}\n' \
  "${COMMIT}" "${BUILD}" "${PIPELINE}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${files}" > latest.json
aws s3 cp latest.json "s3://${BUCKET}/${PIPELINE}/latest.json" --only-show-errors \
  && echo "published; latest.json -> ${COMMIT}"
