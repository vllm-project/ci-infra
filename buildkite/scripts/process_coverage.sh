#!/bin/bash
set -euo pipefail

echo "Starting coverage processing..."

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

# Upload to codecov if token is available
if [ -n "${CODECOV_TOKEN:-}" ]; then
    echo "CODECOV_TOKEN found, proceeding with upload..."
    
    # Install codecov CLI
    echo "Installing codecov CLI..."
    curl -Os https://cli.codecov.io/latest/linux/codecov
    chmod +x codecov
    
    # Upload to codecov
    echo "Uploading coverage to codecov..."
    set +e
    ./codecov upload-process \
        -t "${CODECOV_TOKEN}" \
        -f coverage.xml \
        --git-service github \
        --build "${BUILDKITE_BUILD_NUMBER:-unknown}" \
        --branch "${BUILDKITE_BRANCH:-unknown}" \
        --sha "${BUILDKITE_COMMIT:-unknown}" \
        --slug vllm-project/vllm
    CODECOV_EXIT_CODE=$?
    set -e
    
    if [ ${CODECOV_EXIT_CODE} -eq 0 ]; then
        echo "Codecov upload completed successfully!"
    else
        echo "Codecov upload failed with exit code ${CODECOV_EXIT_CODE}, but continuing..."
    fi
else
    echo "CODECOV_TOKEN not found, skipping codecov upload"
fi

echo "Coverage processing completed!"
