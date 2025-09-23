#!/bin/bash
set -euo pipefail

echo "Starting coverage processing..."
echo "DEBUG: Full environment dump for codecov-related variables:"
env | grep -i codecov || echo "No codecov variables in environment"

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
echo "Fetching CODECOV_TOKEN from AWS Secrets Manager..."
echo "DEBUG: Current CODECOV_TOKEN value: '${CODECOV_TOKEN:-<not set>}'"

# Method 1: Check environment variable first (if passed directly)
if [ -n "${CODECOV_TOKEN:-}" ]; then
    echo "Found CODECOV_TOKEN in environment variable"
    echo "DEBUG: Token length: ${#CODECOV_TOKEN} characters"
    echo "DEBUG: Token first 8 chars: ${CODECOV_TOKEN:0:8}..."
else
    echo "No CODECOV_TOKEN in environment, trying AWS Secrets Manager..."
    echo "DEBUG: About to fetch secret: ci_codecov_token"
    
    # Method 2: Try AWS Secrets Manager
    set +e  # Don't exit on error
    CODECOV_TOKEN=$(aws secretsmanager get-secret-value --secret-id ci_codecov_token --query SecretString --output text 2>&1)
    SECRET_EXIT_CODE=$?
    set -e  # Re-enable exit on error
    
    echo "DEBUG: AWS Secrets Manager exit code: ${SECRET_EXIT_CODE}"
    
    if [ ${SECRET_EXIT_CODE} -ne 0 ]; then
        echo "DEBUG: AWS Secrets Manager failed: '${CODECOV_TOKEN}'"
        echo "WARNING: Failed to fetch codecov token from AWS Secrets Manager"
        CODECOV_TOKEN=""
    else
        echo "DEBUG: AWS Secrets Manager succeeded! Token length: ${#CODECOV_TOKEN}"
        echo "DEBUG: Token first 8 chars: ${CODECOV_TOKEN:0:8}..."
    fi
fi

# Method 3: Fallback - try alternative AWS secret names
if [ -z "${CODECOV_TOKEN}" ]; then
    echo "Primary AWS secret failed, trying alternative secret names..."
    for secret_name in "codecov_token" "ci_codecov" "vllm_codecov_token"; do
        echo "DEBUG: Trying AWS secret: $secret_name"
        set +e
        TEMP_TOKEN=$(aws secretsmanager get-secret-value --secret-id "$secret_name" --query SecretString --output text 2>&1)
        EXIT_CODE=$?
        set -e
        
        if [ ${EXIT_CODE} -eq 0 ] && [ -n "${TEMP_TOKEN}" ] && [[ "${TEMP_TOKEN}" != *"error"* ]]; then
            echo "SUCCESS! Found token with AWS secret name: $secret_name"
            echo "DEBUG: Token length: ${#TEMP_TOKEN} characters"
            echo "DEBUG: Token first 8 chars: ${TEMP_TOKEN:0:8}..."
            CODECOV_TOKEN="${TEMP_TOKEN}"
            break
        else
            echo "DEBUG: Failed to get AWS secret '$secret_name'"
        fi
    done
fi

echo "DEBUG: Final token check..."
echo "DEBUG: CODECOV_TOKEN value: '${CODECOV_TOKEN:-<empty>}'"
echo "DEBUG: Token empty check: $([ -z "${CODECOV_TOKEN}" ] && echo "TRUE - token is empty" || echo "FALSE - token has value")"

if [ -n "${CODECOV_TOKEN}" ]; then
    echo "CODECOV_TOKEN found! Proceeding with codecov upload..."
    echo "DEBUG: Final token length: ${#CODECOV_TOKEN} characters"
    echo "DEBUG: Final token preview: ${CODECOV_TOKEN:0:12}...${CODECOV_TOKEN: -4}"
    
    # Install codecov CLI
    echo "Installing codecov CLI..."
    curl -Os https://cli.codecov.io/latest/linux/codecov
    chmod +x codecov
    
    # Upload to codecov
    echo "Uploading coverage to codecov..."
    echo "DEBUG: Codecov upload command:"
    echo "DEBUG: ./codecov upload-process \\"
    echo "DEBUG:     -t \"${CODECOV_TOKEN:0:12}...\" \\"
    echo "DEBUG:     -f coverage.xml \\"
    echo "DEBUG:     --git-service github \\"
    echo "DEBUG:     --build \"${BUILDKITE_BUILD_NUMBER:-unknown}\" \\"
    echo "DEBUG:     --branch \"${BUILDKITE_BRANCH:-unknown}\" \\"
    echo "DEBUG:     --sha \"${BUILDKITE_COMMIT:-unknown}\" \\"
    echo "DEBUG:     --slug vllm-project/vllm \\"
    echo "DEBUG:     --verbose"
    
    set +e
    ./codecov upload-process \
        -t "${CODECOV_TOKEN}" \
        -f coverage.xml \
        --git-service github \
        --build "${BUILDKITE_BUILD_NUMBER:-unknown}" \
        --branch "${BUILDKITE_BRANCH:-unknown}" \
        --sha "${BUILDKITE_COMMIT:-unknown}" \
        --slug vllm-project/vllm \
        --verbose
    CODECOV_EXIT_CODE=$?
    set -e
    
    echo "DEBUG: Codecov upload exit code: ${CODECOV_EXIT_CODE}"
    
    if [ ${CODECOV_EXIT_CODE} -ne 0 ]; then
        echo "Codecov upload failed with exit code ${CODECOV_EXIT_CODE}, but continuing..."
        exit 0  # Don't fail the build if codecov upload fails
    fi
    
    echo "Codecov upload completed successfully!"
else
    echo "CODECOV_TOKEN not found, skipping codecov upload"
    echo ""
    echo "=== CODECOV SETUP SUGGESTIONS ==="
    echo "1. Set as environment variable in your buildkite pipeline:"
    echo "   env:"
    echo "     CODECOV_TOKEN: \"your-token-here\""
    echo ""
    echo "2. Or add to AWS Secrets Manager with name 'ci_codecov_token'"
    echo "   aws secretsmanager create-secret --name ci_codecov_token --secret-string \"your-token\""
    echo ""
    echo "3. Make sure the terraform is updated with codecov secret access"
    echo "   (see terraform/aws/secrets.tf and main.tf for reference)"
    echo "=================================="
fi

echo "Coverage processing completed!"
