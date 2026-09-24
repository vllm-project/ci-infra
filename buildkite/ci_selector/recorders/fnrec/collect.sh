#!/usr/bin/env bash
# Last step of a recording build: fold every job's Python recordings into one
# coverage table and publish it.
#
# No -e: every gate below exits on its own, and a publishing failure must not
# look like a fold that produced nothing.
set -uo pipefail

BRANCH="${VLLM_CI_BRANCH:-main}"
REPO_URL="https://github.com/vllm-project/ci-infra"
BUCKET="${CI_SELECTOR_BUCKET:-vllm-ci-selector}"
PIPELINE="${BUILDKITE_PIPELINE_SLUG:-ci}"
COMMIT="${BUILDKITE_COMMIT:?}"
BUILD="${BUILDKITE_BUILD_NUMBER:?}"
# The fold reads the source at this commit to check the recorded names.
VLLM_CHECKOUT="${FNREC_VLLM_REPO:-${BUILDKITE_BUILD_CHECKOUT_PATH:?}}"
# A ci-infra checkout to take the table builder from, instead of cloning one.
# For rerunning a fold by hand, and for the tests.
CI_INFRA="${FNREC_CI_INFRA:-}"
# How many jobs the generator armed. Without it the fold has no denominator
# and cannot tell a build that lost its recordings from a small one.
EXPECTED_JOBS="${FNREC_EXPECTED_JOBS:-}"
# Fold another build of the same pipeline instead of this one, as the kernel
# collect can. A recording build's own collect step never runs once one of its
# dependencies is cancelled, allow_dependency_failure or not, and cannot be
# retried. The source must be at this build's commit. The expected count comes
# with it: this build's own describes this build.
FROM=()
if [[ -n "${FNREC_SOURCE_BUILD_ID:-}" ]]; then
  FROM=(--build "${FNREC_SOURCE_BUILD_ID}")
  BUILD="${FNREC_SOURCE_BUILD_NUMBER:?set with FNREC_SOURCE_BUILD_ID}"
  EXPECTED_JOBS="${FNREC_SOURCE_EXPECTED_JOBS:-}"
  echo "folding build ${BUILD} (${FNREC_SOURCE_BUILD_ID}) from build ${BUILDKITE_BUILD_NUMBER}"
fi

WORK="$(mktemp -d)"
cd "${WORK}" || exit 1
trap 'rm -rf -- "${WORK}"' EXIT

echo "--- :satellite: Collecting Python recordings of build ${BUILD}"
if [[ -n "${FNREC_SOURCE_JOBS:-}" ]]; then
  # One search per job: a single search over a whole build's artifacts times
  # out. Two patterns each, since a glob does not cross a slash and a job
  # killed before packing left raw files instead of a tarball.
  printf '%s\n' ${FNREC_SOURCE_JOBS} | xargs -P 8 -I{} sh -c \
    'buildkite-agent artifact download ".fnrec/{}.tar.gz" . "$@" >/dev/null 2>&1
     buildkite-agent artifact download ".fnrec/{}/*" . "$@" >/dev/null 2>&1' _ ${FROM[@]+"${FROM[@]}"}
else
  buildkite-agent artifact download ".fnrec/*" . ${FROM[@]+"${FROM[@]}"} || echo "no packed recordings in this build"
  buildkite-agent artifact download ".fnrec/*/*" . ${FROM[@]+"${FROM[@]}"} || echo "no raw recordings in this build"
fi
n_tar=$(find .fnrec -maxdepth 1 -name '*.tar.gz' 2>/dev/null | wc -l | tr -d ' ')
n_raw=$(find .fnrec -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l | tr -d ' ')
echo "${n_tar} packed and ${n_raw} unpacked jobs"
if [[ "$((n_tar + n_raw))" == "0" ]]; then
  echo "nothing to fold; done"
  exit 0
fi

if [[ -n "${CI_INFRA}" ]]; then
  echo "--- :package: Using the table builder at ${CI_INFRA}"
else
  echo "--- :package: Fetching the table builder"
  git clone --quiet --depth 1 --branch "${BRANCH}" "${REPO_URL}" ci-infra \
    || { echo "cannot clone ${REPO_URL}@${BRANCH}" >&2; exit 1; }
  CI_INFRA="${WORK}/ci-infra"
fi
BUILDER="${CI_INFRA}/buildkite/ci_selector"

# The fold is the ci_selector package, run as the package: through uv, with
# Python 3.12 and the package's own dependencies. Not the agent's python3,
# which is 3.9 on the small CPU queue and has none of them: the package
# imports the pipeline generator (handwritten.py imports amd, which needs
# pyyaml), and under 3.9 the fold exits 0 with every recorded file marked
# unfaithful (no code.co_qualname), a table that can drop nothing.

# Pinned, so the fold does not change between builds. bootstrap.sh pins the
# same one and test_uv_pin.py holds them equal.
UV_VERSION=0.12.15
UV_TARBALL="uv-x86_64-unknown-linux-gnu.tar.gz"
UV_SHA256=f97935763c04be3e692460a7aaeaaab8fc3b78fcf8b389da820b38ae7423a638

uv_is_pinned() { "$1" --version 2>/dev/null | grep -qE "^uv ${UV_VERSION}( |$)"; }

UV_BIN=""
if [[ -n "${VLLM_CI_UV_BIN:-}" ]]; then
  [[ -f "${VLLM_CI_UV_BIN}" && -x "${VLLM_CI_UV_BIN}" ]] \
    || { echo "VLLM_CI_UV_BIN=${VLLM_CI_UV_BIN} is not an executable file" >&2; exit 1; }
  UV_BIN="${VLLM_CI_UV_BIN}"
else
  agent_uv="$(command -v uv || true)"
  if [[ -n "${agent_uv}" ]] && uv_is_pinned "${agent_uv}"; then
    UV_BIN="${agent_uv}"
  fi
fi
if [[ -z "${UV_BIN}" ]]; then
  mkdir -p "${WORK}/uv"
  downloaded=0
  for base in \
    "https://releases.astral.sh/github/uv/releases/download/${UV_VERSION}" \
    "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}"; do
    if curl --fail --remove-on-error --location --silent --show-error \
            --retry 3 --retry-delay 2 --retry-max-time 120 --retry-connrefused \
            --connect-timeout 10 --max-time 300 \
            -o "${WORK}/uv/${UV_TARBALL}" "${base}/${UV_TARBALL}"; then
      downloaded=1
      break
    fi
  done
  (( downloaded )) || { echo "cannot download uv ${UV_VERSION} from either mirror" >&2; exit 1; }
  echo "${UV_SHA256}  ${WORK}/uv/${UV_TARBALL}" | sha256sum --strict -c - >/dev/null \
    || { echo "uv ${UV_VERSION} did not match UV_SHA256 in collect.sh" >&2; exit 1; }
  tar -xzf "${WORK}/uv/${UV_TARBALL}" -C "${WORK}/uv" --strip-components=1 \
    || { echo "cannot extract ${UV_TARBALL}" >&2; exit 1; }
  UV_BIN="${WORK}/uv/uv"
fi
echo "Using uv at ${UV_BIN}"
"${UV_BIN}" --version \
  || { echo "uv at ${UV_BIN} will not run" >&2; exit 1; }

# --locked so the lockfile picks the versions, --python so the interpreter
# matches .python-version.
fold() { "${UV_BIN}" run --quiet --locked --no-dev --python 3.12 --project "${BUILDER}" "$@"; }

echo "--- :table_tennis_paddle_and_ball: Building the coverage table"
# The count the generator armed, so the fold can refuse a build that lost most
# of its recordings. Printed, because a wrong one silently weakens that check.
expected=()
if [[ -n "${EXPECTED_JOBS}" ]]; then
  expected=(--expected-jobs "${EXPECTED_JOBS}")
  echo "expecting ${EXPECTED_JOBS} jobs"
else
  echo "no expected job count; the delivery check cannot fire"
fi
# From here failures exit non-zero: a half-built table must never be published
# as a complete one.
mkdir -p out
fold python -m ci_selector.scripts.build \
  "${VLLM_CHECKOUT}" --fnrec .fnrec --build "${BUILD}" --commit "${COMMIT}" \
  ${expected[@]+"${expected[@]}"} \
  --pipeline "${PIPELINE}" --out out/table.json.gz \
  || { echo "table build failed" >&2; exit 1; }

read -r table_ok table_commit <<<"$(fold python - <<'PY'
import sys
from pathlib import Path
from ci_selector.coverage.table import load

table = load(Path("out/table.json.gz"))
print("yes" if table.available and len(table) else "no", table.commit or "-")
print(table.unavailable, file=sys.stderr)
PY
)"

echo "--- :arrow_up: Uploading the table as an artifact"
(cd out && buildkite-agent artifact upload "*")

# A backstop. Nothing recorded means nothing was delivered, which the check
# above refuses first, so this only catches a table that loads but is empty.
if [[ "${table_ok}" != "yes" ]]; then
  echo "table has no usable rows; not publishing" >&2
  exit 1
fi
# The prefix says which tree this table describes, so the table has to agree.
# Otherwise every fetch refuses it, long after latest.json points at it.
if [[ "${table_commit}" != "${COMMIT}" ]]; then
  echo "table records commit ${table_commit}, this build is ${COMMIT}; not publishing" >&2
  exit 1
fi

# Only main's recordings match the tree PRs are selected against, and only
# postmerge agents can write the bucket. Other branches stop here, green.
if [[ "${BUILDKITE_BRANCH:-main}" != "main" ]]; then
  echo "branch ${BUILDKITE_BRANCH} is not main; not publishing (the artifacts above are the result)"
  exit 0
fi

echo "--- :s3: Publishing to s3://${BUCKET}/${PIPELINE}/fnrec/${COMMIT}/"
if ! command -v aws >/dev/null 2>&1; then
  echo "aws cli not on this agent; the table is an artifact of this job only" >&2
  exit 0
fi
if ! aws sts get-caller-identity >/dev/null 2>&1; then
  echo "no AWS identity on this agent; the table is an artifact of this job only" >&2
  exit 0
fi
if ! aws s3 cp out/table.json.gz "s3://${BUCKET}/${PIPELINE}/fnrec/${COMMIT}/table.json.gz" --only-show-errors; then
  echo "S3 upload failed (bucket or write permission not in place?); the artifact is on this job" >&2
  exit 1
fi
printf '{"commit":"%s","build":%s,"pipeline":"%s","published_at":"%s","files":["table.json.gz"]}\n' \
  "${COMMIT}" "${BUILD}" "${PIPELINE}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > latest.json
aws s3 cp latest.json "s3://${BUCKET}/${PIPELINE}/fnrec/latest.json" --only-show-errors \
  && echo "published; latest.json -> ${COMMIT}"
