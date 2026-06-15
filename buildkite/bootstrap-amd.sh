
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

if [[ -z "${DOCS_ONLY_DISABLE:-}" ]]; then
    DOCS_ONLY_DISABLE=0
fi

if [[ -z "${COV_ENABLED:-}" ]]; then
    COV_ENABLED=0
fi

if [[ -z "${VLLM_CI_SNAPSHOT_ENABLED:-}" ]]; then
    if [[ "${BUILDKITE_SOURCE:-}" == "schedule" ]]; then
        VLLM_CI_SNAPSHOT_ENABLED=1
    else
        VLLM_CI_SNAPSHOT_ENABLED=0
    fi
fi

if [[ -z "${VLLM_CI_SNAPSHOT_REF:-}" ]]; then
    VLLM_CI_SNAPSHOT_REF="main"
fi

if [[ -z "${VLLM_CI_SNAPSHOT_CLOCK:-}" ]]; then
    VLLM_CI_SNAPSHOT_CLOCK="04:00:00"
fi

if [[ -z "${VLLM_CI_SNAPSHOT_TIMEZONE:-}" ]]; then
    VLLM_CI_SNAPSHOT_TIMEZONE="America/Chicago"
fi

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------
fetch_origin_ref() {
    local ref="$1"
    git fetch --no-tags --depth=50 origin "${ref}:refs/remotes/origin/${ref}" >/dev/null 2>&1 || \
        git fetch --no-tags origin "${ref}:refs/remotes/origin/${ref}" >/dev/null 2>&1
}

get_pr_labels() {
    if [[ "${BUILDKITE_PULL_REQUEST:-false}" == "false" ]]; then
        return 0
    fi

    curl -fsSL "https://api.github.com/repos/vllm-project/vllm/pulls/$BUILDKITE_PULL_REQUEST" 2>/dev/null | \
        jq -r '.labels[].name' 2>/dev/null || true
}

join_file_diff() {
    if [[ -z "${1:-}" ]]; then
        return 0
    fi

    printf '%s\n' "$1" | tr -d '\r' | paste -sd'|' -
}

fetch_snapshot_ref() {
    local ref="$1"
    local depth="${VLLM_CI_SNAPSHOT_FETCH_DEPTH:-2000}"
    local remote_ref="refs/remotes/origin/${ref}"

    git fetch --no-tags --depth="${depth}" origin "${ref}:${remote_ref}" >/dev/null 2>&1 || \
        git fetch --no-tags origin "${ref}:${remote_ref}" >/dev/null 2>&1
}

resolve_snapshot_commit() {
    local ref="${VLLM_CI_SNAPSHOT_REF}"
    local snapshot_tz="${VLLM_CI_SNAPSHOT_TIMEZONE}"
    local snapshot_clock="${VLLM_CI_SNAPSHOT_CLOCK}"
    local snapshot_date=""
    local cutoff_epoch=""
    local cutoff_utc=""
    local remote_ref="refs/remotes/origin/${ref}"
    local commit=""

    if [[ -n "${VLLM_CI_SNAPSHOT_DATE:-}" ]]; then
        snapshot_date="${VLLM_CI_SNAPSHOT_DATE}"
    else
        snapshot_date=$(TZ="${snapshot_tz}" date +%F)
    fi

    cutoff_epoch=$(TZ="${snapshot_tz}" date -d "${snapshot_date} ${snapshot_clock}" '+%s')
    cutoff_utc=$(date -u -d "@${cutoff_epoch}" '+%Y-%m-%dT%H:%M:%SZ')
    echo "Resolving ${ref} snapshot at ${snapshot_date} ${snapshot_clock} ${snapshot_tz} (${cutoff_utc})" >&2

    fetch_snapshot_ref "${ref}"
    commit=$(git rev-list -n 1 --before="${cutoff_utc}" "${remote_ref}" 2>/dev/null || true)

    if [[ -z "${commit}" ]]; then
        echo "Snapshot commit not found in fetched history; deepening ${ref} and retrying..." >&2
        git fetch --no-tags origin "${ref}:${remote_ref}" >/dev/null 2>&1 || true
        commit=$(git rev-list -n 1 --before="${cutoff_utc}" "${remote_ref}" 2>/dev/null || true)
    fi

    if [[ -z "${commit}" ]]; then
        echo "ERROR: Could not resolve ${ref} before ${cutoff_utc}" >&2
        exit 1
    fi

    printf '%s\n' "${commit}"
}

checkout_workload_commit() {
    local commit="$1"

    if [[ -z "${commit}" ]]; then
        return 0
    fi

    git fetch --no-tags --depth=1 origin "${commit}" >/dev/null 2>&1 || true
    git checkout --force "${commit}"
    echo "Checked out workload commit: $(git rev-parse HEAD)"
}

# ---------------------------------------------------------------------------
# Git setup: ensure origin/main is available and compute merge base once.
# On K8s (blobless clones with --filter=blob:none), origin/main may not be
# fetched yet if the agent only fetched the PR branch. Also mark the repo
# as safe in case the checkout uid differs from the running uid.
# ---------------------------------------------------------------------------
git config --global --add safe.directory "$(pwd)" 2>/dev/null || true

if git rev-parse --is-shallow-repository 2>/dev/null | grep -q "true"; then
    echo "Shallow repository detected, deepening history..."
    git fetch --no-tags --deepen=50 origin >/dev/null 2>&1 || true
fi

if ! git rev-parse --verify origin/main >/dev/null 2>&1; then
    echo "origin/main not found, fetching..."
    fetch_origin_ref main || true
fi

if [[ "${VLLM_CI_SNAPSHOT_ENABLED}" == "1" ]]; then
    if [[ -z "${VLLM_CI_SNAPSHOT_COMMIT:-}" ]]; then
        VLLM_CI_SNAPSHOT_COMMIT=$(resolve_snapshot_commit)
    fi
    VLLM_CI_WORKLOAD_COMMIT="${VLLM_CI_SNAPSHOT_COMMIT}"
    echo "Using scheduled workload snapshot commit: ${VLLM_CI_WORKLOAD_COMMIT}"
    checkout_workload_commit "${VLLM_CI_WORKLOAD_COMMIT}"
else
    VLLM_CI_WORKLOAD_COMMIT="${VLLM_CI_WORKLOAD_COMMIT:-${BUILDKITE_COMMIT:-HEAD}}"
fi

VLLM_CI_WORKLOAD_IMAGE="${VLLM_CI_WORKLOAD_IMAGE:-rocm/vllm-ci:${VLLM_CI_WORKLOAD_COMMIT}}"
export VLLM_CI_SNAPSHOT_COMMIT="${VLLM_CI_SNAPSHOT_COMMIT:-}"
export VLLM_CI_WORKLOAD_COMMIT
export VLLM_CI_WORKLOAD_IMAGE

if [[ -z "${MERGE_BASE_COMMIT:-}" ]]; then
    MERGE_BASE_COMMIT=$(git merge-base origin/main HEAD 2>/dev/null || echo "")
    if [[ -z "$MERGE_BASE_COMMIT" ]]; then
        echo "WARNING: Could not compute merge base, falling back to run_all=1"
        RUN_ALL=1
        MERGE_BASE_COMMIT="HEAD"
    fi
fi

fail_fast() {
    DISABLE_LABEL="ci-no-fail-fast"
    # If BUILDKITE_PULL_REQUEST != "false", then we check the PR labels using curl and jq
    if [ "$BUILDKITE_PULL_REQUEST" != "false" ]; then
        PR_LABELS=$(get_pr_labels)
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
        PR_LABELS=$(get_pr_labels)
        if [[ $PR_LABELS == *"$RUN_ALL_LABEL"* ]]; then
            echo true
        else
            echo false
        fi
    else
        echo false  # not a PR or BUILDKITE_PULL_REQUEST not set
    fi
}

# ---------------------------------------------------------------------------
# get_diff: compute changed files between commits only (no index staging).
#
# IMPORTANT: Do NOT use "git add ." here. On K8s blobless clones
# (--filter=blob:none), git add . fetches and stages the entire repo,
# producing a diff with every file and eventually exceeding ARG_MAX.
# We only need committed changes between refs.
# ---------------------------------------------------------------------------
get_diff() {
    git diff --name-only --diff-filter=ACMDR "$MERGE_BASE_COMMIT" HEAD 2>/dev/null || echo ""
}

get_diff_main() {
    git diff --name-only --diff-filter=ACMDR HEAD~1 HEAD 2>/dev/null || echo ""
}

# ---------------------------------------------------------------------------
# upload_pipeline: render and upload the Buildkite pipeline YAML
# ---------------------------------------------------------------------------
upload_pipeline() {
    echo "Uploading pipeline..."
    # Install minijinja
    ls .buildkite || buildkite-agent annotate --style error 'Please merge upstream main branch for buildkite CI'
    curl -sSfL https://github.com/mitsuhiko/minijinja/releases/download/2.3.1/minijinja-cli-installer.sh | sh
    TEMPLATE_PATH=".buildkite/test-template-amd.j2"
    CARGO_ENV="${CARGO_HOME:-$HOME/.cargo}/env"
    if [[ ! -f "$CARGO_ENV" ]]; then
        echo "Error: Cargo env file not found at $CARGO_ENV"
        exit 1
    fi
    # shellcheck disable=SC1090
    source "$CARGO_ENV"

    if [[ $BUILDKITE_PIPELINE_SLUG == "fastcheck" ]]; then
        AMD_MIRROR_HW="amdtentative"
    fi
    curl -fsSL -o "$TEMPLATE_PATH" \
        "https://raw.githubusercontent.com/vllm-project/ci-infra/$VLLM_CI_BRANCH/buildkite/test-template-amd.j2?$(date +%s)"


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
    echo "Workload commit: $VLLM_CI_WORKLOAD_COMMIT"
    echo "Workload image: $VLLM_CI_WORKLOAD_IMAGE"

    FAIL_FAST=$(fail_fast)

    cd .buildkite
    (
        set -x
        # Output pipeline.yaml with all blank lines removed
        minijinja-cli test-template-amd.j2 test-amd.yaml \
            -D branch="$BUILDKITE_BRANCH" \
            -D list_file_diff="$LIST_FILE_DIFF" \
            -D run_all="$RUN_ALL" \
            -D nightly="$NIGHTLY" \
            -D mirror_hw="$AMD_MIRROR_HW" \
            -D fail_fast="$FAIL_FAST" \
            -D vllm_use_precompiled="$VLLM_USE_PRECOMPILED" \
            -D vllm_merge_base_commit="$MERGE_BASE_COMMIT" \
            -D cov_enabled="$COV_ENABLED" \
            -D vllm_ci_branch="$VLLM_CI_BRANCH" \
            -D vllm_ci_workload_commit="$VLLM_CI_WORKLOAD_COMMIT" \
            -D vllm_ci_workload_image="$VLLM_CI_WORKLOAD_IMAGE" \
            | sed '/^[[:space:]]*$/d' \
            > pipeline.yaml
    )
    cat pipeline.yaml
    buildkite-agent artifact upload pipeline.yaml
    buildkite-agent pipeline upload pipeline.yaml
    exit 0
}

# ---------------------------------------------------------------------------
# Compute file diff
# ---------------------------------------------------------------------------
if [[ $BUILDKITE_BRANCH == "main" ]] && ! git rev-parse --verify HEAD~1 >/dev/null 2>&1; then
    echo "HEAD~1 not available on main, fetching one more commit..."
    git fetch --no-tags --deepen=1 origin >/dev/null 2>&1 || true
fi

diff_unavailable=0
if [[ $BUILDKITE_BRANCH == "main" ]] && ! git rev-parse --verify HEAD~1 >/dev/null 2>&1; then
    echo "WARNING: Could not resolve HEAD~1 on main, falling back to run_all=1"
    RUN_ALL=1
    diff_unavailable=1
fi

if [[ $BUILDKITE_BRANCH == "main" ]]; then
    if [[ $diff_unavailable -eq 1 ]]; then
        file_diff=""
    else
        file_diff=$(get_diff_main)
    fi
else
    file_diff=$(get_diff)
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
      if [[ "${f#docs/}" != "$f" || "$f" == *.md || "$f" == "mkdocs.yaml" ]]; then
        continue
      else
        docs_only=0
        break
      fi
    done < <(printf '%s\n' "$file_diff" | tr -d '\r')

    if [[ "$docs_only" -eq 1 ]]; then
      buildkite-agent annotate ":memo: CI skipped — docs/Markdown/mkdocs-only changes detected

\`\`\`
$(printf '%s\n' "$file_diff" | tr -d '\r')
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
    "docker/Dockerfile.rocm"
    "docker/Dockerfile.rocm_base"
    "CMakeLists.txt"
    "requirements/common.txt"
    "requirements/rocm.txt"
    "requirements/build/rocm.txt"
    "requirements/test/rocm.txt"
    "setup.py"
    "csrc/"
    "cmake/"
)

ignore_patterns=(
    "csrc/cpu"
    "cmake/cpu_extension.cmake"
)

while IFS= read -r file; do
    [[ -z "$file" ]] && continue
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
done < <(printf '%s\n' "$file_diff" | tr -d '\r')

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
    export VLLM_USE_PRECOMPILED=1
    echo "No critical changes, using precompiled wheels"
fi

# Build LIST_FILE_DIFF from the already-computed file_diff.
# When run_all=1, the jinja template ignores list_file_diff (all tests are
# unblocked unconditionally), so use a short sentinel to avoid exceeding
# ARG_MAX when the diff is large (K8s fresh clones).
if [[ $RUN_ALL -eq 1 ]]; then
    LIST_FILE_DIFF="run_all"
else
    LIST_FILE_DIFF=$(join_file_diff "$file_diff")
fi

upload_pipeline
