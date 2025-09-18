#!/bin/bash
set -euo pipefail

# Hardcode single test
RUN_ALL=0
LIST_FILE_DIFF="tests/benchmarks/test_latency_cli.py"

# Default values
NIGHTLY="${NIGHTLY:-0}"
VLLM_CI_BRANCH="${VLLM_CI_BRANCH:-main}"
AMD_MIRROR_HW="${AMD_MIRROR_HW:-amdproduction}"

fail_fast() {
    DISABLE_LABEL="ci-no-fail-fast"
    if [ "$BUILDKITE_PULL_REQUEST" != "false" ]; then
        PR_LABELS=$(curl -s "https://api.github.com/repos/vllm-project/vllm/pulls/$BUILDKITE_PULL_REQUEST" | jq -r '.labels[].name')
        if [[ $PR_LABELS == *"$DISABLE_LABEL"* ]]; then
            echo false
        else
            echo true
        fi
    else
        echo false
    fi
}

upload_pipeline() {
    echo "Uploading pipeline..."

    # Ensure minijinja installed
    curl -sSfL https://github.com/mitsuhiko/minijinja/releases/download/2.3.1/minijinja-cli-installer.sh | sh
    source /var/lib/buildkite-agent/.cargo/env

    FAIL_FAST=$(fail_fast)

    # Use custom template for single test
    cd .buildkite
    set -x
    minijinja-cli test-template-ci-myscript.j2 test-pipeline.yaml \
        -D branch="$BUILDKITE_BRANCH" \
        -D list_file_diff="$LIST_FILE_DIFF" \
        -D run_all="$RUN_ALL" \
        -D nightly="$NIGHTLY" \
        -D mirror_hw="$AMD_MIRROR_HW" \
        -D fail_fast="$FAIL_FAST" \
        -D vllm_use_precompiled="${VLLM_USE_PRECOMPILED:-1}" \
        | sed '/^[[:space:]]*$/d' \
        > pipeline.yaml

    cat pipeline.yaml
    buildkite-agent artifact upload pipeline.yaml
    buildkite-agent pipeline upload pipeline.yaml
    exit 0
}

# Default to precompiled wheels
export VLLM_USE_PRECOMPILED="${VLLM_USE_PRECOMPILED:-1}"

# Directly upload pipeline for single test
upload_pipeline

