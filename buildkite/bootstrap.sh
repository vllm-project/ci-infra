#!/bin/bash

set -euo pipefail

if [[ -z "${RUN_ALL:-}" ]]; then
    RUN_ALL=0
fi

if [[ -z "${NIGHTLY:-}" ]]; then
    NIGHTLY=0
fi

if [[ -z "${VLLM_CI_BRANCH:-}" ]]; then
    VLLM_CI_BRANCH="main"
fi

if [[ -z "${AMD_MIRROR_HW:-}" ]]; then
    AMD_MIRROR_HW="amdproduction"
fi

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

upload_pipeline() {
    echo "Uploading pipeline..."
    # Install minijinja
    ls .buildkite || buildkite-agent annotate --style error 'Please merge upstream main branch for buildkite CI'
    curl -sSfL https://github.com/mitsuhiko/minijinja/releases/download/2.3.1/minijinja-cli-installer.sh | sh
    source /var/lib/buildkite-agent/.cargo/env

    if [[ $BUILDKITE_PIPELINE_SLUG == "fastcheck" ]]; then
        curl -o .buildkite/test-template.j2 \
            https://raw.githubusercontent.com/vllm-project/ci-infra/"$VLLM_CI_BRANCH"/buildkite/test-template-fastcheck.j2
    else
        curl -o .buildkite/test-template.j2 \
            "https://raw.githubusercontent.com/vllm-project/ci-infra/$VLLM_CI_BRANCH/buildkite/test-template-ci.j2?$(date +%s)"
    fi


    # (WIP) Use pipeline generator instead of jinja template
    if [ -e ".buildkite/pipeline_generator/pipeline_generator.py" ]; then
        python -m pip install click pydantic
        python .buildkite/pipeline_generator/pipeline_generator.py --run_all=$RUN_ALL --list_file_diff="$LIST_FILE_DIFF" --nightly="$NIGHTLY" --mirror_hw="$AMD_MIRROR_HW"
        buildkite-agent pipeline upload .buildkite/pipeline.yaml
        exit 0
    fi
    echo "List file diff: $LIST_FILE_DIFF"
    echo "Run all: $RUN_ALL"
    echo "Nightly: $NIGHTLY"
    echo "AMD Mirror HW: $AMD_MIRROR_HW"

    FAIL_FAST=$(fail_fast)

    cd .buildkite
    (
        set -x
        # Output pipeline.yaml with all blank lines removed
        minijinja-cli test-template.j2 test-pipeline.yaml \
            -D branch="$BUILDKITE_BRANCH" \
            -D list_file_diff="$LIST_FILE_DIFF" \
            -D run_all="$RUN_ALL" \
            -D nightly="$NIGHTLY" \
            -D mirror_hw="$AMD_MIRROR_HW" \
            -D fail_fast="$FAIL_FAST" \
            -D vllm_use_precompiled="$VLLM_USE_PRECOMPILED" \
            -D skip_image_build="$SKIP_IMAGE_BUILD" \
            -D docker_image_override="$DOCKER_IMAGE_OVERRIDE" \
            | sed '/^[[:space:]]*$/d' \
            > pipeline.yaml
    )
    cat pipeline.yaml
    buildkite-agent artifact upload pipeline.yaml
    buildkite-agent pipeline upload pipeline.yaml
    exit 0
}

get_diff() {
    $(git add .)
    echo $(git diff --name-only --diff-filter=ACMDR $(git merge-base origin/main HEAD))
}

get_diff_main() {
    $(git add .)
    echo $(git diff --name-only --diff-filter=ACMDR HEAD~1)
}

file_diff=$(get_diff)
if [[ $BUILDKITE_BRANCH == "main" ]]; then
    file_diff=$(get_diff_main)
fi

patterns=(
    ".buildkite/test-pipeline"
    "docker/Dockerfile"
    "CMakeLists.txt"
    "requirements/common.txt"
    "requirements/cuda.txt"
    "requirements/build.txt"
    "requirements/test.txt"
    "setup.py"
    "csrc/"
    "cmake/"
)

ignore_patterns=(
    "docker/Dockerfile."
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

# Decide whether to use precompiled wheels
# Relies on existing patterns array as a basis.
if [[ -n "${VLLM_USE_PRECOMPILED:-}" ]]; then
    echo "VLLM_USE_PRECOMPILED is already set to: $VLLM_USE_PRECOMPILED"
elif [[ $RUN_ALL -eq 1 || "${BUILDKITE_BRANCH}" == "main" ]]; then
    export VLLM_USE_PRECOMPILED=0
    echo "Detected critical changes, building wheels from source"
else
    export VLLM_USE_PRECOMPILED=1
    echo "No critical changes, using precompiled wheels"
fi

# Decide whether to skip building docker images (pull & mount code instead)
# Honor manual override if provided.
if [[ -n "${SKIP_IMAGE_BUILD:-}" ]]; then
    echo "SKIP_IMAGE_BUILD is preset to: ${SKIP_IMAGE_BUILD}"
else
    # Auto decision:
    # - No critical changes (RUN_ALL==0)
    # - VLLM_USE_PRECOMPILED==1
    if [[ "${VLLM_USE_PRECOMPILED:-}" == "1" && "$RUN_ALL" -eq 0 ]]; then
        SKIP_IMAGE_BUILD=1
    else
        SKIP_IMAGE_BUILD=0
    fi
fi
echo "Final SKIP_IMAGE_BUILD=${SKIP_IMAGE_BUILD} (RUN_ALL=${RUN_ALL}, VLLM_USE_PRECOMPILED=${VLLM_USE_PRECOMPILED:-unset})"

# Select Docker image based on latest common ancestor (LCA) commit between current branch and main
LCA_COMMIT=""
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    LCA_COMMIT=$(git merge-base origin/main HEAD)
fi
if [[ -n "$LCA_COMMIT" ]]; then
    DOCKER_IMAGE_OVERRIDE="public.ecr.aws/q9t5s3a7/vllm-ci-postmerge-repo:$LCA_COMMIT"
    echo "Using Docker image for LCA commit: $DOCKER_IMAGE_OVERRIDE"
else
    DOCKER_IMAGE_OVERRIDE="public.ecr.aws/q9t5s3a7/vllm-ci-postmerge-repo:latest"
    echo "Could not determine LCA commit, using latest Docker image: $DOCKER_IMAGE_OVERRIDE"
fi

################## end WIP #####################

LIST_FILE_DIFF=$(get_diff | tr ' ' '|')
if [[ $BUILDKITE_BRANCH == "main" ]]; then
    LIST_FILE_DIFF=$(get_diff_main | tr ' ' '|')
fi
upload_pipeline
