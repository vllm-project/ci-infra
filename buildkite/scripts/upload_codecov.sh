#!/bin/bash
set -e

# Script to upload coverage to Codecov
# Usage: upload_codecov.sh "Step Label"
#
# Coverage Architecture Notes:
# - Tests import vllm from: /usr/local/lib/python3.12/dist-packages/vllm/ (installed package)
# - Source code exists at: /vllm-workspace/src/vllm/ (for python_only_compile.sh test)
# - Coverage tracks the installed package location during test execution
# - Path mapping in .coveragerc normalizes paths to vllm/ for reporting

STEP_LABEL="${1:-unknown}"

# Convert step label to flag format (lowercase, replace special chars with underscores)
FLAG=$(echo "$STEP_LABEL" | tr '[:upper:]' '[:lower:]' | sed 's/[() %,+]/_/g' | sed 's/__*/_/g' | sed 's/^_//;s/_$//')

# Skip coverage upload for problematic tests
if [ "$FLAG" = "multi-modal_models_test_standard" ]; then
    echo "Skipping coverage upload for $STEP_LABEL (known multi-directory test issue)"
    exit 0
fi

# Find all .coverage.* files in the workspace
COVERAGE_DB_FILES=$(find /vllm-workspace -name ".coverage.*" -type f 2>/dev/null || true)

if [ -z "$COVERAGE_DB_FILES" ]; then
    echo "No .coverage.* files found in /vllm-workspace, skipping upload"
    exit 0
fi

echo "Found $(echo "$COVERAGE_DB_FILES" | wc -l) coverage file(s) to process"

# Change to /vllm-workspace to combine coverage files
cd /vllm-workspace

# Move all .coverage.* files to the current directory so coverage combine can find them
# Note: coverage combine only looks in the current directory, not subdirectories
# Using cp -n to skip files that are already in the target directory
for cov_file in $COVERAGE_DB_FILES; do
    if [ -f "$cov_file" ]; then
        cp -n "$cov_file" . 2>/dev/null || true
    fi
done

# Combine all coverage database files into a single .coverage file
# This will apply the [paths] remapping from .coveragerc to normalize paths to vllm/
echo "Combining coverage files..."
COMBINE_OUTPUT=$(python3 -m coverage combine --keep 2>&1)
COMBINE_EXIT=$?

echo "$COMBINE_OUTPUT"

# Check if it's the "No data to combine" error (coverage exits with 0 even for this!)
if echo "$COMBINE_OUTPUT" | grep -q "No data to combine"; then
    echo "Warning: No coverage data found - skipping upload"
    exit 0
fi

if [ $COMBINE_EXIT -ne 0 ]; then
    echo "Error: coverage combine failed with status $COMBINE_EXIT"
    exit 1
fi

# Check if combine was successful
if [ ! -f .coverage ]; then
    echo "Error: Failed to combine coverage files - .coverage not created"
    exit 1
fi
echo "Successfully combined coverage files"

# Generate XML report from the combined coverage data
# This will use the path mappings from .coveragerc to normalize paths
echo "Generating XML coverage report..."
python3 -m coverage xml -o coverage.xml

if [ ! -f coverage.xml ]; then
    echo "Error: Failed to generate coverage.xml"
    exit 1
fi

# Count total files in coverage report
TOTAL_FILES=$(grep -c '<class.*filename=' coverage.xml || echo "0")
echo "Generated coverage.xml with $TOTAL_FILES files"

# Download codecov CLI if not present
if [ ! -f codecov ]; then
    echo "Downloading codecov CLI..."
    if ! curl -Os https://cli.codecov.io/latest/linux/codecov; then
        echo "Warning: Failed to download codecov CLI"
        exit 1
    fi
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
# Let codecov find all coverage files and use codecov.yml fixes to normalize paths
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
echo "Uploading to codecov..."
./codecov "${CODECOV_ARGS[@]}" || echo "Warning: codecov upload failed"

exit 0
