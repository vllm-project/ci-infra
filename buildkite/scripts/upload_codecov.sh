#!/bin/bash
set -e

# Script to upload coverage to Codecov
# Usage: upload_codecov.sh "Step Label"

STEP_LABEL="${1:-unknown}"

# Convert step label to flag format (lowercase, replace special chars with underscores)
FLAG=$(echo "$STEP_LABEL" | tr '[:upper:]' '[:lower:]' | sed 's/[() %,+]/_/g' | sed 's/__*/_/g' | sed 's/^_//;s/_$//')

# Skip coverage upload for problematic tests
if [ "$FLAG" = "multi-modal_models_test_standard" ]; then
    echo "Skipping coverage upload for $STEP_LABEL (known multi-directory test issue)"
    exit 0
fi

# Find and normalize ALL coverage.xml files in the workspace
# This handles cases where tests run from different directories (e.g., whisper tests with cd ..)
COVERAGE_FILES=$(find /vllm-workspace -name "coverage.xml" -type f 2>/dev/null || true)

if [ -z "$COVERAGE_FILES" ]; then
    echo "No coverage.xml files found in /vllm-workspace, skipping upload"
    exit 0
fi

echo "Found coverage.xml files:"
echo "$COVERAGE_FILES"

# Normalize paths in ALL coverage.xml files
for cov_file in $COVERAGE_FILES; do
    echo ""
    echo "Processing: $cov_file"
    echo "Sample paths before normalization:"
    grep 'filename=' "$cov_file" | head -3 || true
    
    # Normalize filenames to ensure consistent paths across uploads
    # Map any site/dist-packages and workspace-relative paths to canonical "vllm/"
    if sed -i \
        -e 's@filename="[^"]*/site-packages/vllm/@filename="vllm/@g' \
        -e 's@filename="[^"]*/dist-packages/vllm/@filename="vllm/@g' \
        -e 's@filename="/vllm-workspace/vllm/@filename="vllm/@g' \
        -e 's@filename="\./vllm/@filename="vllm/@g' \
        -e 's@filename="\.\./vllm/@filename="vllm/@g' \
        "$cov_file" 2>/dev/null; then
        echo "✓ Path normalization successful for $cov_file"
    else
        echo "⚠ Warning: sed path normalization failed for $cov_file"
    fi
    
    echo "Sample paths after normalization:"
    grep 'filename=' "$cov_file" | head -3 || true
done

# Use coverage.xml in current directory for upload
if [ ! -f coverage.xml ]; then
    echo "Warning: No coverage.xml in current directory $(pwd), using first found file"
    FIRST_COV=$(echo "$COVERAGE_FILES" | head -1)
    ln -s "$FIRST_COV" coverage.xml || cp "$FIRST_COV" coverage.xml
fi

# Ensure Codecov does not re-generate coverage from local DBs anywhere; upload XML as-is
find /vllm-workspace -type f -name ".coverage*" -delete 2>/dev/null || true
rm -f .coverage .coverage.* 2>/dev/null || true

# Download codecov CLI if not present
if [ ! -f codecov ]; then
    curl -Os https://cli.codecov.io/latest/linux/codecov
    chmod +x codecov
fi

# Determine slug (handle fork PRs)
DEFAULT_SLUG="vllm-project/vllm"
UPLOAD_SLUG="$DEFAULT_SLUG"
if [ -n "${BUILDKITE_PULL_REQUEST:-}" ] && [ "${BUILDKITE_PULL_REQUEST}" != "false" ] && [ -n "${BUILDKITE_PULL_REQUEST_REPO:-}" ]; then
    # Parse owner/repo from git URL (git@github.com:owner/repo.git or https://github.com/owner/repo.git)
    UPLOAD_SLUG=$(echo "${BUILDKITE_PULL_REQUEST_REPO}" | sed -E 's#(git@|https?://)([^/:]+)[:/]([^/]+/[^/.]+)(\.git)?$#\3#')
    # Fallback to default on parse failure
    if ! echo "$UPLOAD_SLUG" | grep -q '/'; then
        UPLOAD_SLUG="$DEFAULT_SLUG"
    fi
fi

echo "Uploading coverage for slug: $UPLOAD_SLUG, sha: ${BUILDKITE_COMMIT:-unknown}, branch: ${BUILDKITE_BRANCH:-unknown}, pr: ${BUILDKITE_PULL_REQUEST:-none}"

# Only require token for upstream slug; forks typically don't need one
if [ "$UPLOAD_SLUG" = "$DEFAULT_SLUG" ] && [ -z "${CODECOV_TOKEN:-}" ]; then
    echo "CODECOV_TOKEN not set for upstream slug, skipping upload"
    exit 0
fi

# Build Codecov args
# Use coverage-upload directory to ensure codecov only finds our single combined file
CODECOV_ARGS=(upload-process -f coverage.xml --git-service github \
    --build "${BUILDKITE_BUILD_NUMBER:-unknown}" \
    --branch "${BUILDKITE_BRANCH:-unknown}" \
    --sha "${BUILDKITE_COMMIT:-unknown}" \
    --slug "$UPLOAD_SLUG" \
    --flag "$FLAG" \
    --name "$STEP_LABEL" \
    --dir /vllm-workspace \
    --disable-search)

# Include PR number if available to help Codecov associate fork PRs
if [ -n "${BUILDKITE_PULL_REQUEST:-}" ] && [ "${BUILDKITE_PULL_REQUEST}" != "false" ]; then
    CODECOV_ARGS=("${CODECOV_ARGS[@]}" --pr "${BUILDKITE_PULL_REQUEST}")
fi

# Upload to codecov
./codecov "${CODECOV_ARGS[@]}" || true

exit 0
