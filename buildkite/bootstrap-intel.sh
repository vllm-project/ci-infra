#!/bin/bash

set -euo pipefail

if [[ -z "${RUN_ALL:-}" ]]; then
    RUN_ALL=0
fi

if [[ -z "${NIGHTLY:-}" ]]; then
    NIGHTLY=0
fi

if [[ -z "${TORCH_NIGHTLY:-}" ]]; then
    TORCH_NIGHTLY=0
fi

if [[ -z "${VLLM_CI_BRANCH:-}" ]]; then
    VLLM_CI_BRANCH="main"
fi

if [[ -z "${VLLM_CI_REPO:-}" ]]; then
    VLLM_CI_REPO="vllm-project/ci-infra"
fi

if [[ -z "${AMD_MIRROR_HW:-}" ]]; then
    AMD_MIRROR_HW="amdproduction"
fi

if [[ -z "${DOCS_ONLY_DISABLE:-}" ]]; then
    DOCS_ONLY_DISABLE=0
fi

if [[ -z "${COV_ENABLED:-}" ]]; then
    COV_ENABLED=0
fi

PIPELINE_CONFIG_PATH="${PIPELINE_CONFIG_PATH:-.buildkite/ci_config_intel.yaml}"
OUTPUT_PIPELINE_PATH="${OUTPUT_PIPELINE_PATH:-.buildkite/pipeline.yaml}"
SCRIPT_DIR="$(cd -- "$(dirname "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
GENERATOR_MAIN="${SCRIPT_DIR}/pipeline_generator/main.py"

fail_fast() {
    DISABLE_LABEL="ci-no-fail-fast"
    # If BUILDKITE_PULL_REQUEST != "false", then we check the PR labels using curl and jq
    if [ "$BUILDKITE_PULL_REQUEST" != "false" ]; then
        PR_LABELS=$(curl -s "https://api.github.com/repos/vllm-project/vllm/pulls/$BUILDKITE_PULL_REQUEST" | jq -r '.labels[].name')
        if [[ $PR_LABELS == *"$DISABLE_LABEL"* ]]; then
            echo false
        else
            echo true
        fi
    else
        echo false  # not a PR or BUILDKITE_PULL_REQUEST not set
    fi
}

check_run_all_label() {
    RUN_ALL_LABEL="ready-run-all-tests"
    # If BUILDKITE_PULL_REQUEST != "false", then we check the PR labels using curl and jq
    if [ "$BUILDKITE_PULL_REQUEST" != "false" ]; then
        PR_LABELS=$(curl -s "https://api.github.com/repos/vllm-project/vllm/pulls/$BUILDKITE_PULL_REQUEST" | jq -r '.labels[].name')
        if [[ $PR_LABELS == *"$RUN_ALL_LABEL"* ]]; then
            echo true
        else
            echo false
        fi
    else
        echo false  # not a PR or BUILDKITE_PULL_REQUEST not set
    fi
}

clean_docker_tag() {
    # Function to replace invalid characters in Docker image tags and truncate to 128 chars
    # Valid characters: a-z, A-Z, 0-9, _, ., -
    local input="$1"
    echo "$input" | sed 's/[^a-zA-Z0-9._-]/_/g' | cut -c1-128
}

fetch_origin_ref() {
    local ref="$1"
    git fetch --no-tags --depth=50 origin "${ref}:refs/remotes/origin/${ref}" >/dev/null 2>&1 || \
        git fetch --no-tags origin "${ref}:refs/remotes/origin/${ref}" >/dev/null 2>&1
}

git config --global --add safe.directory "$(pwd)" 2>/dev/null || true

BASE_BRANCH="${BUILDKITE_PULL_REQUEST_BASE_BRANCH:-main}"
if [[ "${BUILDKITE_PULL_REQUEST:-false}" == "false" ]]; then
    BASE_BRANCH="main"
fi

fetch_origin_ref "$BASE_BRANCH" || true

if [[ -z "${MERGE_BASE_COMMIT:-}" ]]; then
    MERGE_BASE_COMMIT=$(git merge-base "origin/${BASE_BRANCH}" HEAD 2>/dev/null || echo "")
    if [[ -z "$MERGE_BASE_COMMIT" ]]; then
        echo "WARNING: Could not compute merge base, falling back to run_all=1"
        RUN_ALL=1
        MERGE_BASE_COMMIT="HEAD"
    fi
fi
export MERGE_BASE_COMMIT

resolve_ecr_cache_vars() {
    # Resolve ECR cache-from, cache-to using buildkite environment variables:
    #  -  BUILDKITE_BRANCH
    #  -  BUILDKITE_PULL_REQUEST
    #  -  BUILDKITE_PULL_REQUEST_BASE_BRANCH
    # Export environment variables:
    #  -  CACHE_FROM: primary cache source
    #  -  CACHE_FROM_BASE_BRANCH: secondary cache source
    #  -  CACHE_FROM_MAIN: fallback cache source
    #  -  CACHE_TO: cache destination
    # Note: CACHE_FROM, CACHE_FROM_BASE_BRANCH, CACHE_FROM_MAIN could be the same.
    #     This is intended behavior to allow BuildKit to merge all possible cache source
    #     to maximize cache hit potential, see https://docs.docker.com/build/cache/backends/#multiple-caches

    # Define ECR repository URLs for test and main cache
    local TEST_CACHE_ECR="936637512419.dkr.ecr.us-east-1.amazonaws.com/vllm-ci-test-cache"
    local MAIN_CACHE_ECR="936637512419.dkr.ecr.us-east-1.amazonaws.com/vllm-ci-postmerge-cache"

    if [[ "$BUILDKITE_PULL_REQUEST" == "false" ]]; then
        if [[ "$BUILDKITE_BRANCH" == "main" ]]; then
            local cache="${MAIN_CACHE_ECR}:latest"
        else
            local clean_branch=$(clean_docker_tag "$BUILDKITE_BRANCH")
            local cache="${TEST_CACHE_ECR}:${clean_branch}"
        fi
        CACHE_TO="$cache"
        CACHE_FROM="$cache"
        CACHE_FROM_BASE_BRANCH="$cache"
    else
        CACHE_TO="${TEST_CACHE_ECR}:pr-${BUILDKITE_PULL_REQUEST}"
        CACHE_FROM="${TEST_CACHE_ECR}:pr-${BUILDKITE_PULL_REQUEST}"
        if [[ "$BUILDKITE_PULL_REQUEST_BASE_BRANCH" == "main" ]]; then
            CACHE_FROM_BASE_BRANCH="${MAIN_CACHE_ECR}:latest"
        else
            local clean_base=$(clean_docker_tag "$BUILDKITE_PULL_REQUEST_BASE_BRANCH")
            CACHE_FROM_BASE_BRANCH="${TEST_CACHE_ECR}:${clean_base}"
        fi
    fi

    CACHE_FROM_MAIN="${MAIN_CACHE_ECR}:latest"
    export CACHE_FROM CACHE_FROM_BASE_BRANCH CACHE_FROM_MAIN CACHE_TO
}

upload_pipeline() {
    echo "Uploading Intel pipeline..."

    if [[ ! -e "$GENERATOR_MAIN" ]]; then
        buildkite-agent annotate --style error "Intel pipeline generator not found at $GENERATOR_MAIN"
        exit 1
    fi

    if [[ ! -f "$PIPELINE_CONFIG_PATH" ]]; then
        buildkite-agent annotate --style error "Intel pipeline config not found at $PIPELINE_CONFIG_PATH"
        exit 1
    fi

    echo "List file diff: $LIST_FILE_DIFF"
    echo "Run all: $RUN_ALL"
    echo "Nightly: $NIGHTLY"
    echo "Torch Nightly: $TORCH_NIGHTLY"

    FAIL_FAST=$(fail_fast)

    # Resolve CACHE_FROM and CACHE_TO for ECR Registry Caching
    resolve_ecr_cache_vars
    if [[ -z "${CACHE_FROM:-}" ]] || [[ -z "${CACHE_FROM_BASE_BRANCH:-}" ]] || [[ -z "${CACHE_FROM_MAIN:-}" ]] || [[ -z "${CACHE_TO:-}" ]]; then
        echo "Error: CACHE_FROM, CACHE_FROM_BASE_BRANCH, CACHE_FROM_MAIN, or CACHE_TO not set after resolve_ecr_cache_vars"
        exit 1
    else
        echo "Resolved CACHE_FROM: ${CACHE_FROM}"
        echo "Resolved CACHE_FROM_BASE_BRANCH: ${CACHE_FROM_BASE_BRANCH}"
        echo "Resolved CACHE_FROM_MAIN: ${CACHE_FROM_MAIN}"
        echo "Resolved CACHE_TO: ${CACHE_TO}"
    fi

    python -m pip install click pydantic requests pyyaml

    if [[ "${BUILDKITE_PULL_REQUEST:-false}" != "false" ]]; then
        echo "Exporting trusted pipeline generator from origin/${VLLM_CI_BRANCH:-main}..."
        TMP_GEN_DIR=$(mktemp -d /tmp/intel_gen.XXXXXX)
        trap 'rm -rf "$TMP_GEN_DIR"' EXIT INT TERM
        git archive "origin/${VLLM_CI_BRANCH:-main}" buildkite/pipeline_generator | tar -x -C "$TMP_GEN_DIR"
        GENERATOR_MAIN="$TMP_GEN_DIR/buildkite/pipeline_generator/main.py"
    fi

    python "$GENERATOR_MAIN" \
        --pipeline_config_path "$PIPELINE_CONFIG_PATH" \
        --output_file_path "$OUTPUT_PIPELINE_PATH"

    if [[ -f ".buildkite/.docs_only" ]]; then
        echo "[docs-only] Generator skipped CI. Exiting before pipeline upload."
        exit 0
    fi

    if [[ ! -f "$OUTPUT_PIPELINE_PATH" ]]; then
        echo "Error: $OUTPUT_PIPELINE_PATH was not generated"
        exit 1
    fi

    cat "$OUTPUT_PIPELINE_PATH"
    buildkite-agent artifact upload "$OUTPUT_PIPELINE_PATH"
    buildkite-agent pipeline upload "$OUTPUT_PIPELINE_PATH"
    exit 0
}

get_diff() {
    git diff --name-only --diff-filter=ACMDR "$MERGE_BASE_COMMIT" HEAD 2>/dev/null || echo ""
}

get_diff_main() {
    git diff --name-only --diff-filter=ACMDR HEAD~1 HEAD 2>/dev/null || echo ""
}

file_diff=$(get_diff)
if [[ $BUILDKITE_BRANCH == "main" ]]; then
    file_diff=$(get_diff_main)
fi

# ----------------------------------------------------------------------
# Early exit start: skip pipeline if conditions are met
# ----------------------------------------------------------------------

# skip pipeline if *every* changed file is docs/** OR **/*.md OR mkdocs.yaml
if [[ "${DOCS_ONLY_DISABLE}" != "1" ]]; then
  if [[ -n "${file_diff:-}" ]]; then
    docs_only=1
    # Iterate robustly over newline-separated paths
    while IFS= read -r f; do
      [[ -z "$f" ]] && continue
      # Match any of: docs/**  OR  **/*.md  OR  mkdocs.yaml
      # Using prefix check for docs/ so nested paths match (no need for globstar).
      if [[ "${f#docs/}" != "$f" || "$f" == *.md || "$f" == "mkdocs.yaml" ]]; then
        continue
      else
        docs_only=0
        break
      fi
    done < <(printf '%s\n' "$file_diff" | tr ' ' '\n' | tr -d '\r')

    if [[ "$docs_only" -eq 1 ]]; then
      buildkite-agent annotate ":memo: CI skipped — docs/Markdown/mkdocs-only changes detected

\`\`\`
$(printf '%s\n' "$file_diff" | tr ' ' '\n')
\`\`\`" --style "info" || true
      echo "[docs-only] All changes are docs/**, *.md, or mkdocs.yaml. Exiting before pipeline upload."
      exit 0
    fi
  fi
fi

# ----------------------------------------------------------------------
# Early exit end
# ----------------------------------------------------------------------

patterns=(
    "docker/Dockerfile"
    "CMakeLists.txt"
    "requirements/common.txt"
    "requirements/xpu.txt"
    "requirements/build.txt"
    "requirements/test.txt"
    "setup.py"
    "csrc/"
    "cmake/"
)

ignore_patterns=(
    "docker/Dockerfile."
    "csrc/cpu"
    "csrc/rocm"
    "cmake/hipify.py"
    "cmake/cpu_extension.cmake"
)

for file in $file_diff; do
    # First check if file matches any pattern
    matches_pattern=0
    for pattern in "${patterns[@]}"; do
        if [[ $file == $pattern* ]] || [[ $file == $pattern ]]; then
            matches_pattern=1
            break
        fi
    done

    # If file matches pattern, check it's not in ignore patterns
    if [[ $matches_pattern -eq 1 ]]; then
        matches_ignore=0
        for ignore in "${ignore_patterns[@]}"; do
            if [[ $file == $ignore* ]] || [[ $file == $ignore ]]; then
                matches_ignore=1
                break
            fi
        done

        if [[ $matches_ignore -eq 0 ]]; then
            RUN_ALL=1
            echo "Found changes: $file. Run all tests"
            break
        fi
    fi
done

# Check for ready-run-all-tests label
LABEL_RUN_ALL=$(check_run_all_label)
if [[ $LABEL_RUN_ALL == true ]]; then
    RUN_ALL=1
    NIGHTLY=1
    echo "Found 'ready-run-all-tests' label. Running all tests including optional tests."
fi

# Decide whether to use precompiled wheels
# Relies on existing patterns array as a basis.
if [[ -n "${VLLM_USE_PRECOMPILED:-}" ]]; then
    echo "VLLM_USE_PRECOMPILED is already set to: $VLLM_USE_PRECOMPILED"
elif [[ $RUN_ALL -eq 1 ]]; then
    export VLLM_USE_PRECOMPILED=0
    echo "Detected critical changes, building wheels from source"
else
    echo "No critical changes, trying to use precompiled wheels"
    # check whether we have precompiled wheels available for the merge base commit
    # this might happen when:
    # (1) the main commit is very new and wheels are not built yet
    # (2) the merge base commit is somehow not on main branch (is that possible?)
    # (3) (maybe later) we have retired some too old precompiled wheels
    # (4) unfortunately, the commit fails to build
    meta_url="https://wheels.vllm.ai/${MERGE_BASE_COMMIT}/vllm/metadata.json"
    echo "Checking for precompiled wheel metadata at: $meta_url"
    if curl --silent --head --fail "$meta_url"; then
        echo "Precompiled wheels are available for commit ${MERGE_BASE_COMMIT}"
        export VLLM_USE_PRECOMPILED=1
    else
        echo "Precompiled wheels are NOT available for commit ${MERGE_BASE_COMMIT}, forcing build from source"
        export VLLM_USE_PRECOMPILED=0
    fi
fi

if [[ $RUN_ALL -eq 1 ]]; then
    LIST_FILE_DIFF="run_all"
else
    LIST_FILE_DIFF=$(printf '%s\n' "$file_diff" | tr -d '\r' | paste -sd'|' -)
fi

upload_pipeline
