#!/bin/bash
# ci-bake.sh - Wrapper script for Docker buildx bake CI builds
#
# This script handles the common setup for running docker buildx bake:
# - Downloads ci.hcl from ci-infra
# - Detects and uses local buildkitd if available (custom AMI with warm cache)
# - Falls back to docker-container driver on regular instances
# - Runs bake with --print for debugging
# - Runs the actual build
#
# Usage:
#   ci-bake.sh [TARGET]
#
# Environment variables (all optional, with sensible defaults):
#   CI_HCL_URL          - URL to ci.hcl (default: from ci-infra main branch)
#   VLLM_CI_BRANCH      - ci-infra branch to use (default: main)
#   VLLM_BAKE_FILE      - Path to vLLM's bake file (default: docker/docker-bake.hcl)
#   BUILDER_NAME        - Name for buildx builder (default: vllm-builder)
#
# Build configuration (passed through to bake via environment):
#   BUILDKITE_COMMIT    - Git commit (auto-detected from Buildkite)
#   PARENT_COMMIT       - Parent commit (HEAD~1) for cache fallback (auto-computed)
#   IMAGE_TAG           - Primary image tag
#   IMAGE_TAG_LATEST    - Latest tag (optional)
#   CACHE_FROM          - Cache source
#   CACHE_FROM_BASE     - Base branch cache source
#   CACHE_FROM_MAIN     - Main branch cache source
#   CACHE_TO            - Cache destination
#   VLLM_USE_PRECOMPILED    - Use precompiled wheels
#   VLLM_MERGE_BASE_COMMIT  - Merge base commit for precompiled

set -euo pipefail

# Check if image already exists (skip build if it does)
if [[ -n "${IMAGE_TAG:-}" ]]; then
    echo "--- :mag: Checking if image exists"
    if docker manifest inspect "${IMAGE_TAG}" >/dev/null 2>&1; then
        echo "Image already exists: ${IMAGE_TAG}"
        echo "Skipping build"
        exit 0
    fi
    echo "Image not found, proceeding with build"
fi

# Configuration with defaults
TARGET="${1:-test-ci}"
CI_HCL_URL="${CI_HCL_URL:-https://raw.githubusercontent.com/vllm-project/ci-infra/main/docker/ci.hcl}"
VLLM_BAKE_FILE="${VLLM_BAKE_FILE:-docker/docker-bake.hcl}"
BUILDER_NAME="${BUILDER_NAME:-vllm-builder}"
CI_HCL_PATH="/tmp/ci.hcl"
BUILDKIT_SOCKET="/run/buildkit/buildkitd.sock"

echo "--- :docker: Setting up Docker buildx bake"
echo "Target: ${TARGET}"
echo "CI HCL URL: ${CI_HCL_URL}"
echo "vLLM bake file: ${VLLM_BAKE_FILE}"

# Print instance/AMI debug info
echo ""
echo "=== Debug: Instance Information ==="
# Get IMDSv2 token
if TOKEN=$(curl -s -X PUT "http://169.254.169.254/latest/api/token" \
    -H "X-aws-ec2-metadata-token-ttl-seconds: 21600" 2>/dev/null); then
    AMI_ID=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" \
        http://169.254.169.254/latest/meta-data/ami-id 2>/dev/null || echo "unknown")
    INSTANCE_TYPE=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" \
        http://169.254.169.254/latest/meta-data/instance-type 2>/dev/null || echo "unknown")
    INSTANCE_ID=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" \
        http://169.254.169.254/latest/meta-data/instance-id 2>/dev/null || echo "unknown")
    AZ=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" \
        http://169.254.169.254/latest/meta-data/placement/availability-zone 2>/dev/null || echo "unknown")
    echo "AMI ID:        ${AMI_ID}"
    echo "Instance Type: ${INSTANCE_TYPE}"
    echo "Instance ID:   ${INSTANCE_ID}"
    echo "AZ:            ${AZ}"
else
    echo "Not running on EC2 or IMDS not available"
fi
# Check for warm cache AMI (marker file baked into custom AMI)
if [[ -f /etc/vllm-ami-info ]]; then
    echo "Cache:         warm (custom vLLM AMI)"
    cat /etc/vllm-ami-info
else
    echo "Cache:         cold (standard AMI)"
fi
echo "==================================="
echo ""

# Check if vLLM bake file exists
if [[ ! -f "${VLLM_BAKE_FILE}" ]]; then
    echo "Error: vLLM bake file not found at ${VLLM_BAKE_FILE}"
    echo "Make sure you're running from the vLLM repository root"
    exit 1
fi

# Download ci.hcl
echo "--- :arrow_down: Downloading ci.hcl"
curl -sSfL -o "${CI_HCL_PATH}" "${CI_HCL_URL}"
echo "Downloaded to ${CI_HCL_PATH}"

# Set up buildx builder
# Priority: 1) local buildkitd socket (custom AMI) 2) existing builder 3) new docker-container builder
echo "--- :buildkite: Setting up buildx builder"

if [[ -S "${BUILDKIT_SOCKET}" ]]; then
    # Custom AMI with standalone buildkitd - use remote driver for warm cache
    echo "✅ Found local buildkitd socket at ${BUILDKIT_SOCKET}"
    echo "Using remote driver to connect to buildkitd (warm cache available)"

    # Check if baked-vllm-builder already exists and is using the socket
    if docker buildx inspect baked-vllm-builder >/dev/null 2>&1; then
        echo "Using existing baked-vllm-builder"
        docker buildx use baked-vllm-builder
    else
        echo "Creating baked-vllm-builder with remote driver"
        docker buildx create \
            --name baked-vllm-builder \
            --driver remote \
            --use \
            "unix://${BUILDKIT_SOCKET}"
    fi
    docker buildx inspect --bootstrap
elif docker buildx inspect "${BUILDER_NAME}" >/dev/null 2>&1; then
    # Existing builder available
    echo "Using existing builder: ${BUILDER_NAME}"
    docker buildx use "${BUILDER_NAME}"
    docker buildx inspect --bootstrap
else
    # No local buildkitd, no existing builder - create new docker-container builder
    echo "No local buildkitd found, using docker-container driver"
    docker buildx create --name "${BUILDER_NAME}" --driver docker-container --use
    docker buildx inspect --bootstrap
fi

# Show builder info
echo "Active builder:"
docker buildx ls | grep -E '^\*|^NAME' || docker buildx ls

# Compute parent commit for cache fallback (if not already set)
if [[ -z "${PARENT_COMMIT:-}" ]]; then
    PARENT_COMMIT=$(git rev-parse HEAD~1 2>/dev/null || echo "")
    if [[ -n "${PARENT_COMMIT}" ]]; then
        echo "Computed parent commit for cache fallback: ${PARENT_COMMIT}"
        export PARENT_COMMIT
    else
        echo "Could not determine parent commit (may be first commit in repo)"
    fi
else
    echo "Using provided PARENT_COMMIT: ${PARENT_COMMIT}"
fi

# Print resolved configuration for debugging and save for artifact upload
echo "--- :page_facing_up: Resolved bake configuration"
BAKE_CONFIG_FILE="bake-config-build-${BUILDKITE_BUILD_NUMBER:-local}.json"
docker buildx bake -f "${VLLM_BAKE_FILE}" -f "${CI_HCL_PATH}" --print "${TARGET}" | tee "${BAKE_CONFIG_FILE}" || true
echo "Saved bake config to ${BAKE_CONFIG_FILE}"

# Run the actual build
echo "--- :docker: Building ${TARGET}"
docker buildx bake -f "${VLLM_BAKE_FILE}" -f "${CI_HCL_PATH}" --progress plain "${TARGET}"

echo "--- :white_check_mark: Build complete"

# ---------------------------------------------------------------------------
# Wheel artifact upload.
#
# If the bake target included export-wheel-rocm (via a *-ci-with-wheel group),
# the wheel is already extracted to ./wheel-export/. Compress and upload it
# as a Buildkite artifact so test jobs can assemble images locally from
# ci_base + wheel instead of pulling large data from Docker Hub.
#
# If ./wheel-export/ doesn't exist, this section is a no-op.
#
# Artifact path: artifacts/vllm-wheel/*.whl.zst
# ---------------------------------------------------------------------------
WHEEL_DIR="./wheel-export"
if [[ -d "${WHEEL_DIR}" ]] && ls "${WHEEL_DIR}"/*.whl >/dev/null 2>&1; then
    echo "--- :package: Compressing and uploading vLLM wheel"

    ARTIFACT_DIR="artifacts/vllm-wheel"
    mkdir -p "${ARTIFACT_DIR}"

    for whl in "${WHEEL_DIR}"/*.whl; do
        [ -f "${whl}" ] || continue
        WHL_NAME=$(basename "${whl}")
        echo "Compressing ${WHL_NAME}..."
        zstd -19 -T0 "${whl}" -o "${ARTIFACT_DIR}/${WHL_NAME}.zst"
        echo "  Original: $(du -sh "${whl}" | cut -f1)"
        echo "  Compressed: $(du -sh "${ARTIFACT_DIR}/${WHL_NAME}.zst" | cut -f1)"
    done

    if [ -d "${WHEEL_DIR}/requirements" ]; then
        cp -r "${WHEEL_DIR}/requirements" "${ARTIFACT_DIR}/"
    fi
    if [ -d "${WHEEL_DIR}/tests" ]; then
        tar cf - -C "${WHEEL_DIR}" tests | zstd -9 -T0 -o "${ARTIFACT_DIR}/tests.tar.zst"
        echo "  Tests archive: $(du -sh "${ARTIFACT_DIR}/tests.tar.zst" | cut -f1)"
    fi

    if command -v buildkite-agent >/dev/null 2>&1; then
        buildkite-agent artifact upload "${ARTIFACT_DIR}/*"
        echo "Wheel artifacts uploaded"
    else
        echo "Not in Buildkite, skipping artifact upload"
    fi

    rm -rf "${WHEEL_DIR}"
fi
