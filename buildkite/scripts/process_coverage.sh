#!/bin/bash
set -euo pipefail

# Allow custom secret name via environment variable
CODECOV_SECRET_NAME="${CODECOV_SECRET_NAME:-CODECOV_TOKEN}"

echo "Starting coverage processing..."
echo "Using secret name: ${CODECOV_SECRET_NAME}"

# Download coverage files from buildkite artifacts
echo "Downloading coverage artifacts..."
buildkite-agent artifact download '.coverage.*' . || echo 'No coverage files found'

# Check if we have any coverage files
if ! ls .coverage.* 1> /dev/null 2>&1; then
    echo 'No coverage files found. Skipping coverage report generation.'
    echo 'This may indicate that all pytest steps failed or coverage was not enabled.'
    exit 0
fi

echo 'Coverage files found, generating reports...'

# Combine all coverage files
echo "Combining coverage files..."
python3 -m coverage combine

# Generate HTML report
echo "Generating HTML coverage report..."
python3 -m coverage html -d coverage_html_report

# Generate XML report for codecov
echo "Generating XML coverage report..."
python3 -m coverage xml -o coverage.xml

# Create tarball of HTML report
echo "Creating HTML report archive..."
tar -czf coverage_report.tar.gz coverage_html_report/

# Upload HTML report as buildkite artifact
echo "Uploading HTML report to buildkite artifacts..."
buildkite-agent artifact upload coverage_report.tar.gz

# Print coverage summary
echo "Coverage summary:"
python3 -m coverage report

# Upload to codecov - try multiple methods to get token
echo "Fetching CODECOV_TOKEN from buildkite secrets..."

# Method 1: Check environment variable first (if passed directly)
if [ -n "${CODECOV_TOKEN:-}" ]; then
    echo "Found CODECOV_TOKEN in environment variable"
else
    echo "No CODECOV_TOKEN in environment, trying buildkite secrets..."
    
    # Method 2: Try buildkite-agent secret get
    CODECOV_TOKEN=$(buildkite-agent secret get "${CODECOV_SECRET_NAME}" 2>/dev/null || echo "")
fi

# Method 3: Try alternative secret names
if [ -z "${CODECOV_TOKEN}" ]; then
    echo "buildkite-agent secret get failed, trying alternative secret names..."
    CODECOV_TOKEN=$(buildkite-agent secret get codecov-token 2>/dev/null || echo "")
fi

# Method 4: Try with different case
if [ -z "${CODECOV_TOKEN}" ]; then
    echo "codecov-token failed, trying lowercase..."
    CODECOV_TOKEN=$(buildkite-agent secret get codecov_token 2>/dev/null || echo "")
fi

# Method 5: Try other common variations
if [ -z "${CODECOV_TOKEN}" ]; then
    echo "codecov_token failed, trying other variations..."
    for name in "CODECOV" "codecov" "codecov_upload_token" "CODECOV_UPLOAD_TOKEN"; do
        echo "Trying secret name: $name"
        CODECOV_TOKEN=$(buildkite-agent secret get "$name" 2>/dev/null || echo "")
        if [ -n "${CODECOV_TOKEN}" ]; then
            echo "Success! Found token with name: $name"
            break
        fi
    done
fi

# Debug: Show buildkite-agent secret help and try to list secrets
echo "Available buildkite-agent commands:"
buildkite-agent --help | grep -E "(secret|env)" || echo "No secret commands found"

echo "Buildkite-agent secret subcommands:"
buildkite-agent secret --help 2>/dev/null || echo "Secret help not available"

echo "Attempting to get more verbose error information:"
echo "Trying ${CODECOV_SECRET_NAME} with verbose output:"
buildkite-agent secret get "${CODECOV_SECRET_NAME}" 2>&1 || echo "Failed to get ${CODECOV_SECRET_NAME}"

echo "Checking if we can list secrets:"
buildkite-agent secret list 2>&1 || echo "Cannot list secrets (may need permissions)"

if [ -n "${CODECOV_TOKEN}" ]; then
    echo "CODECOV_TOKEN found! Proceeding with codecov upload..."
    
    # Install codecov CLI
    echo "Installing codecov CLI..."
    curl -Os https://cli.codecov.io/latest/linux/codecov
    chmod +x codecov
    
    # Upload to codecov
    echo "Uploading coverage to codecov..."
    ./codecov upload-process \
        -t "${CODECOV_TOKEN}" \
        -f coverage.xml \
        --git-service github \
        --build "${BUILDKITE_BUILD_NUMBER:-unknown}" \
        --branch "${BUILDKITE_BRANCH:-unknown}" \
        --sha "${BUILDKITE_COMMIT:-unknown}" \
        --slug vllm-project/vllm \
        --verbose || {
            echo "Codecov upload failed, but continuing..."
            exit 0  # Don't fail the build if codecov upload fails
        }
    
    echo "Codecov upload completed successfully!"
else
    echo "CODECOV_TOKEN not found in buildkite secrets, skipping codecov upload"
    echo "Make sure to add CODECOV_TOKEN to buildkite secrets manager"
fi

echo "Coverage processing completed!"
