#!/bin/bash
# Bootstrap for the CUDA/CPU CI pipeline.

set -euo pipefail

main() {
    # Buildkite sets these to point a build at a ci-infra ref under test.
    CI_INFRA_REPO="${VLLM_CI_REPO:-vllm-project/ci-infra}"
    CI_INFRA_REF="${VLLM_CI_BRANCH:-main}"
    # Absent unless a machine image baked uv in, which is not an error.
    IMAGE_UV_BIN="${VLLM_CI_UV_BIN:-/opt/uv/uv}"

    PIPELINE_CONFIG_PATH=.buildkite/ci_config.yaml
    OUTPUT_PIPELINE_PATH=.buildkite/pipeline.yaml

    UV_VERSION=0.12.15
    UV_TARBALL="uv-x86_64-unknown-linux-gnu.tar.gz"
    UV_SHA256=f97935763c04be3e692460a7aaeaaab8fc3b78fcf8b389da820b38ae7423a638

    fail() {
        echo "ERROR: $1" >&2
        # Own context, so this does not erase the generator's own annotations.
        timeout 30 buildkite-agent annotate "$1" --style error --context bootstrap || true
        exit 1
    }

    [[ "${CI_INFRA_REPO}" =~ ^vllm-project/[A-Za-z0-9._-]+$ ]] ||
        fail "VLLM_CI_REPO must be a vllm-project repo (got ${CI_INFRA_REPO})."
    [[ "${CI_INFRA_REF}" != -* ]] ||
        fail "VLLM_CI_BRANCH must not start with '-' (got ${CI_INFRA_REF})."

    VLLM_CHECKOUT="$(pwd)"
    WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/vllm-ci-bootstrap.XXXXXXXX")"
    trap 'rm -rf "${WORK_DIR}" || true' EXIT

    # The generator runs `git add .` in the checkout, so a temp dir inside it
    # would enter the changed-file list and break docs-only detection.
    [[ "${WORK_DIR}" != "${VLLM_CHECKOUT}"/* ]] ||
        fail "TMPDIR is inside the vLLM checkout (${WORK_DIR})."

    # ----------------------------------------------------------------------
    # uv
    # ----------------------------------------------------------------------
    echo "--- :package: Setting up uv"
    if [[ -f "${IMAGE_UV_BIN}" && -x "${IMAGE_UV_BIN}" ]]; then
        UV_BIN="${IMAGE_UV_BIN}"
        echo "Using the image's uv at ${UV_BIN}"
        "${UV_BIN}" --version || fail "Image uv at ${UV_BIN} will not execute."
    else
        UV_DL="${WORK_DIR}/uv/${UV_TARBALL}"
        mkdir -p "${WORK_DIR}/uv"
        uv_downloaded=0
        for base in \
            "https://releases.astral.sh/github/uv/releases/download/${UV_VERSION}" \
            "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}"; do
            if curl --fail --remove-on-error --location --silent --show-error \
                    --retry 3 --retry-delay 2 --retry-max-time 120 --retry-connrefused \
                    --connect-timeout 10 --max-time 300 \
                    -o "${UV_DL}" "${base}/${UV_TARBALL}"; then
                uv_downloaded=1
                break
            fi
        done
        (( uv_downloaded )) || fail "Could not download uv ${UV_VERSION} from either mirror."
        echo "${UV_SHA256}  ${UV_DL}" | sha256sum --strict -c - >/dev/null ||
            fail "uv ${UV_VERSION} did not match UV_SHA256 in buildkite/bootstrap.sh."
        tar -xzf "${UV_DL}" -C "${WORK_DIR}/uv" --strip-components=1 ||
            fail "Could not extract ${UV_TARBALL}."
        UV_BIN="${WORK_DIR}/uv/uv"
        "${UV_BIN}" --version || fail "Downloaded uv at ${UV_BIN} will not execute."
    fi

    # ----------------------------------------------------------------------
    # ci-infra: the tree uv syncs from. Nothing is on disk when this starts.
    # ----------------------------------------------------------------------
    echo "--- :git: Fetching ${CI_INFRA_REPO} at ${CI_INFRA_REF}"
    CI_INFRA_DIR="${WORK_DIR}/ci-infra"
    CI_INFRA_URL="https://github.com/${CI_INFRA_REPO}.git"

    for attempt in 1 2 3; do
        if git init --quiet "${CI_INFRA_DIR}" &&
            git -C "${CI_INFRA_DIR}" -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=60 \
                fetch --depth=1 --no-tags "${CI_INFRA_URL}" "${CI_INFRA_REF}" &&
            git -C "${CI_INFRA_DIR}" checkout --quiet FETCH_HEAD; then
            break
        fi
        rm -rf "${CI_INFRA_DIR}"
        sleep $((attempt * 3))
    done
    if [[ ! -d "${CI_INFRA_DIR}/.git" ]]; then
        msg="Could not fetch ${CI_INFRA_REPO} at ${CI_INFRA_REF}."
        [[ -n "${VLLM_CI_BRANCH:-}" || -n "${VLLM_CI_REPO:-}" ]] &&
            msg+=" Check VLLM_CI_BRANCH and VLLM_CI_REPO name something that exists."
        fail "${msg}"
    fi
    echo "ci-infra ${CI_INFRA_REF} at $(git -C "${CI_INFRA_DIR}" rev-parse HEAD)"

    # ----------------------------------------------------------------------
    # The declared environment: the lockfile decides versions, not the agent.
    # ----------------------------------------------------------------------
    echo "--- :snake: Building the environment"
    SYNC_LOG="${WORK_DIR}/uv-sync.log"
    if ! "${UV_BIN}" sync --locked --no-dev --package pipeline-generator \
            --project "${CI_INFRA_DIR}/buildkite" 2>&1 | tee "${SYNC_LOG}"; then
        # Only blame the lockfile when uv did.
        grep -q "needs to be updated" "${SYNC_LOG}" &&
            fail "buildkite/uv.lock is out of date. Run 'uv lock' in buildkite/ and commit the result."
        fail "uv sync failed, see the output above."
    fi
    GENERATOR_BIN="${CI_INFRA_DIR}/buildkite/.venv/bin/pipeline-generator"
    [[ -x "${GENERATOR_BIN}" ]] || fail "uv sync did not produce ${GENERATOR_BIN}."

    # ----------------------------------------------------------------------
    # Generate and upload. Every other job in the build comes from this.
    # ----------------------------------------------------------------------
    echo "--- :buildkite: Generating the pipeline"
    [[ -f "${PIPELINE_CONFIG_PATH}" ]] ||
        fail "${PIPELINE_CONFIG_PATH} not found. Merge upstream main into this branch."
    # The generator shells out to git, which refuses a checkout it does not own.
    git config --global --add safe.directory "${VLLM_CHECKOUT}" ||
        fail "Could not mark ${VLLM_CHECKOUT} as a git safe.directory."

    DOCS_ONLY_MARKER="$(dirname "${OUTPUT_PIPELINE_PATH}")/.docs_only"
    # A committed one would skip CI on every build, so only trust a marker
    # this run produced.
    rm -f "${DOCS_ONLY_MARKER}"

    "${GENERATOR_BIN}" \
        --pipeline_config_path "${PIPELINE_CONFIG_PATH}" \
        --output_file_path "${OUTPUT_PIPELINE_PATH}" ||
        fail "Pipeline generation failed, see the output above."

    if [[ -f "${DOCS_ONLY_MARKER}" ]]; then
        exit 0
    fi

    [[ -f "${OUTPUT_PIPELINE_PATH}" ]] || fail "${OUTPUT_PIPELINE_PATH} was not generated."

    buildkite-agent artifact upload "${OUTPUT_PIPELINE_PATH}" ||
        echo "WARNING: artifact upload failed, continuing" >&2
    buildkite-agent pipeline upload "${OUTPUT_PIPELINE_PATH}" ||
        fail "pipeline upload failed, so this build has no steps."
}

main "$@"
