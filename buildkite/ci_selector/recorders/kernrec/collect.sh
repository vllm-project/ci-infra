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
# job, whatever state they are in, for debugging. Each backend publishes
# independently, only when both its table and map validate
# (table has rows, map has objects and no failure reason) and the agent has
# AWS credentials:
#
#   s3://$CI_SELECTOR_BUCKET/<pipeline>/<commit>/kernel_table.json.gz
#   s3://$CI_SELECTOR_BUCKET/<pipeline>/<commit>/kernel_symbol_map.json.gz
#   s3://$CI_SELECTOR_BUCKET/<pipeline>/latest.json        -> {commit, build, ...}
# ROCm uses .rocm.json.gz filenames and its own latest.rocm.json pointer.
#
# Nothing reaches S3 unless its backend pair is complete. Several builds can collect
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
for map in kernel_symbol_map.json.gz kernel_symbol_map.rocm.json.gz; do
  buildkite-agent artifact download "$map" . ${FROM[@]+"${FROM[@]}"} || echo "no $map in this build"
done
n_files=$(find .kernrec -name 'kern.*.txt' 2>/dev/null | wc -l | tr -d ' ')
n_jobs=$(find .kernrec -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l | tr -d ' ')
echo "${n_files} recording files from ${n_jobs} jobs"
if [[ "${n_jobs}" == "0" ]]; then
  echo "nothing to fold; done"
  exit 0
fi

echo "--- :table_tennis_paddle_and_ball: Building the kernel table"
# From here on a failure is a failure: soft_fail keeps the build green, but
# a half-built result must never be published as if it were complete.
curl -sSfL --retry 3 -o kernel_table.py "${RAW}/kernel_table.py" || { echo "cannot fetch kernel_table.py" >&2; exit 1; }
mkdir -p out
publish=()
for backend in cuda rocm; do
  suffix=""; [[ "$backend" == rocm ]] && suffix=.rocm
  table="kernel_table${suffix}.json.gz"
  map="kernel_symbol_map${suffix}.json.gz"
  if ! "${KERNREC_PYTHON:-python3}" kernel_table.py build --kernrec .kernrec --backend "$backend" \
      --build "${BUILD}" --commit "${COMMIT}" --pipeline "${PIPELINE}" --out "out/$table"; then
    echo "$backend table build failed" >&2
    continue
  fi
  "${KERNREC_PYTHON:-python3}" kernel_table.py show "out/$table" --top 10 || true
  [[ ! -f "$map" ]] || mv "$map" out/
  usable=$("${KERNREC_PYTHON:-python3}" - "$backend" "out/$table" "out/$map" "$COMMIT" <<'PY'
import gzip
import json
import sys
try:
    backend, table, symbols, commit = sys.argv[1:]
    with gzip.open(table, "rt") as stream:
        table = json.load(stream)
    with gzip.open(symbols, "rt") as stream:
        symbols = json.load(stream)
    valid = (
        bool(table.get("rows"))
        and all(row.get("backend", "cuda") in {backend, "unknown"}
                for row in table["rows"].values())
        and bool(symbols.get("objects"))
        and not symbols.get("reason")
        and symbols.get("backend", "cuda") == backend
        and symbols.get("commit") == commit
    )
    print("yes" if valid else "no")
except (OSError, EOFError, ValueError, TypeError, AttributeError):
    print("no")
PY
  )
  if [[ "$usable" == yes ]]; then
    mkdir -p "publish/$backend"
    cp "out/$table" "out/$map" "publish/$backend/"
    publish+=("$backend")
  else
    echo "$backend has no valid table/map pair; retaining artifacts without publishing" >&2
  fi
done

echo "--- :arrow_up: Uploading as build artifacts"
(cd out && buildkite-agent artifact upload "*")

# Both gates come before anything touches S3. The commit prefix is written
# as a unit: a rerun of this commit with a broken half must leave the good
# pair (and latest.json, which may already point here) exactly as it was.
if [[ ${#publish[@]} == 0 ]]; then
  echo "no valid backend pair; existing published pairs remain unchanged" >&2
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
status=0
for backend in "${publish[@]}"; do
  suffix=""; [[ "$backend" == rocm ]] && suffix=.rocm
  latest="latest${suffix}.json"
  if ! aws s3 cp "publish/$backend/" "s3://${BUCKET}/${PIPELINE}/kernrec/${COMMIT}/" --recursive --only-show-errors; then
    echo "$backend upload failed; its pointer is unchanged" >&2
    status=1
    continue
  fi
  "${KERNREC_PYTHON:-python3}" - "$backend" "$COMMIT" "$BUILD" "$PIPELINE" "$latest" <<'PY'
import json
import sys
import time
from pathlib import Path
backend, commit, build, pipeline, latest = sys.argv[1:]
data = {"backend": backend, "commit": commit, "build": int(build),
        "pipeline": pipeline, "published_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "files": sorted(path.name for path in Path("publish", backend).iterdir())}
Path(latest).write_text(json.dumps(data, indent=2) + "\n")
PY
  aws s3 cp "$latest" "s3://${BUCKET}/${PIPELINE}/kernrec/$latest" --only-show-errors \
    && echo "published; $latest -> ${COMMIT}" || status=1
done
exit "$status"
