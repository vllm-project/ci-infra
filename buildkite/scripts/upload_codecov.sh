#!/bin/bash
set -e

# Script to upload coverage to Codecov
# Usage: upload_codecov.sh "Step Label"

STEP_LABEL="${1:-unknown}"

# Convert step label to flag format (lowercase, replace special chars with underscores)
FLAG=$(echo "$STEP_LABEL" | tr '[:upper:]' '[:lower:]' | sed 's/[() %,+]/_/g' | sed 's/__*/_/g' | sed 's/^_//;s/_$//')

# Check if codecov token and coverage.xml exist
if [ -z "${CODECOV_TOKEN:-}" ]; then
    echo "CODECOV_TOKEN not set, skipping upload"
    exit 0
fi

if [ ! -f coverage.xml ]; then
    echo "coverage.xml not found, skipping upload"
    exit 0
fi

# Download codecov CLI if not present
if [ ! -f codecov ]; then
    curl -Os https://cli.codecov.io/latest/linux/codecov
    chmod +x codecov
fi

# Upload to codecov
./codecov upload-process \
    -t "${CODECOV_TOKEN}" \
    -f coverage.xml \
    --git-service github \
    --build "${BUILDKITE_BUILD_NUMBER:-unknown}" \
    --branch "${BUILDKITE_BRANCH:-unknown}" \
    --sha "${BUILDKITE_COMMIT:-unknown}" \
    --slug vllm-project/vllm \
    --flag "$FLAG" \
    --name "$STEP_LABEL" \
    --dir /vllm-workspace || true

exit 0
