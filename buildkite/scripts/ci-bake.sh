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
#
# For commit-tagged images (rocm/vllm-ci:$COMMIT), the tag is unique per
# commit, so "exists = already built" is correct.
#
# For stable-tagged images (rocm/vllm-dev:ci_base), the tag always exists
# after the first weekly build. To detect staleness, we compare a hash of
# the ci_base-affecting source files against a label on the remote image.
# If the hashes match, the image is current and we skip. If they differ
# (or the label is missing), we rebuild.
if [[ -n "${IMAGE_TAG:-}" && "${FORCE_BUILD:-0}" != "1" ]]; then
    echo "--- :mag: Checking if image exists"
    if docker manifest inspect "${IMAGE_TAG}" >/dev/null 2>&1; then
        if [[ -n "${CI_BASE_CONTENT_FILES:-}" ]]; then
            LOCAL_HASH=$(cat ${CI_BASE_CONTENT_FILES} 2>/dev/null | sha256sum | cut -d' ' -f1)
            echo "Local ci_base content hash: ${LOCAL_HASH:0:16}..."

            REMOTE_HASH=$(docker buildx imagetools inspect "${IMAGE_TAG}" --raw 2>/dev/null \
                | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    if data.get('manifests'):
        digest = data['manifests'][0]['digest']
        import subprocess
        r = subprocess.run(
            ['docker', 'buildx', 'imagetools', 'inspect',
             '${IMAGE_TAG}@' + digest, '--raw'],
            capture_output=True, text=True)
        data = json.loads(r.stdout)
    labels = data.get('config', {}).get('Labels', {})
    print(labels.get('vllm.ci_base.content_hash', ''))
except Exception:
    print('')
" 2>/dev/null || echo "")

            if [[ -n "${REMOTE_HASH}" ]]; then
                echo "Remote ci_base content hash: ${REMOTE_HASH:0:16}..."
                if [[ "${LOCAL_HASH}" == "${REMOTE_HASH}" ]]; then
                    echo "Content hashes match -- ci_base is current"
                    echo "Skipping build"
                    exit 0
                else
                    echo "Content hashes DIFFER -- ci_base is stale, rebuilding"
                fi
            else
                echo "Remote image has no content hash label -- rebuilding to add it"
            fi
        else
            echo "Image already exists: ${IMAGE_TAG}"
            echo "Skipping build"
            exit 0
        fi
    else
        echo "Image not found, proceeding with build"
    fi
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

# Deepen shallow clones so HEAD~1 and merge-base are available.
# Buildkite agents often clone with --depth=1; without deepening, git rev-parse
# HEAD~1 and git merge-base both silently fail, disabling the per-commit cache layers.
if git rev-parse --is-shallow-repository 2>/dev/null | grep -q "true"; then
    echo "Shallow clone detected — deepening for cache key computation"
    git fetch --deepen=1 origin 2>/dev/null || true
fi

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

# Compute merge-base with main for an additional cache fallback layer.
# Useful for long-lived PRs where parent-commit cache may be missing but the
# merge-base (a real main commit) maps to a warm :rocm-latest snapshot.
if [[ -z "${VLLM_MERGE_BASE_COMMIT:-}" ]]; then
    git fetch --depth=1 origin main 2>/dev/null || true
    VLLM_MERGE_BASE_COMMIT=$(git merge-base HEAD origin/main 2>/dev/null || echo "")
    if [[ -n "${VLLM_MERGE_BASE_COMMIT}" ]]; then
        echo "Computed merge base commit for cache fallback: ${VLLM_MERGE_BASE_COMMIT}"
        export VLLM_MERGE_BASE_COMMIT
    else
        echo "Could not determine merge base (will skip that cache layer)"
    fi
else
    echo "Using provided VLLM_MERGE_BASE_COMMIT: ${VLLM_MERGE_BASE_COMMIT}"
fi

# Compute and export ci_base content hash (if content files are specified).
# This hash gets embedded as a label in the ci_base image via the bake file's
# CI_BASE_CONTENT_HASH variable, so future builds can compare without rebuilding.
if [[ -n "${CI_BASE_CONTENT_FILES:-}" ]]; then
    CI_BASE_CONTENT_HASH=$(cat ${CI_BASE_CONTENT_FILES} 2>/dev/null | sha256sum | cut -d' ' -f1)
    export CI_BASE_CONTENT_HASH
    echo "ci_base content hash: ${CI_BASE_CONTENT_HASH:0:16}... (will be embedded as image label)"
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
# Artifact paths (arch-namespaced + legacy):
#   artifacts/vllm-wheel-{arch}/*.whl.zst   (per-arch, e.g. vllm-wheel-gfx942)
#   artifacts/vllm-wheel-fat/*.whl.zst      (fat multi-arch build)
#   artifacts/vllm-wheel/*.whl.zst          (legacy path, always written)
# ---------------------------------------------------------------------------
WHEEL_DIR="./wheel-export"
if [[ -d "${WHEEL_DIR}" ]] && ls "${WHEEL_DIR}"/*.whl >/dev/null 2>&1; then
    echo "--- :package: Compressing and uploading vLLM wheel"

    # Determine architecture suffix from PYTORCH_ROCM_ARCH.
    # Single arch (no semicolons) -> e.g. "gfx942"; multi-arch -> "fat".
    WHEEL_ARCH_SUFFIX="fat"
    if [[ -n "${PYTORCH_ROCM_ARCH:-}" ]] && [[ "${PYTORCH_ROCM_ARCH}" != *";"* ]]; then
        WHEEL_ARCH_SUFFIX="${PYTORCH_ROCM_ARCH}"
    fi
    echo "Wheel arch suffix: ${WHEEL_ARCH_SUFFIX} (PYTORCH_ROCM_ARCH=${PYTORCH_ROCM_ARCH:-unset})"

    ARTIFACT_DIR="artifacts/vllm-wheel-${WHEEL_ARCH_SUFFIX}"
    ARTIFACT_DIR_LEGACY="artifacts/vllm-wheel"
    mkdir -p "${ARTIFACT_DIR}" "${ARTIFACT_DIR_LEGACY}"

    for whl in "${WHEEL_DIR}"/*.whl; do
        [ -f "${whl}" ] || continue
        WHL_NAME=$(basename "${whl}")
        echo "Compressing ${WHL_NAME}..."
        zstd -19 -T0 "${whl}" -o "${ARTIFACT_DIR}/${WHL_NAME}.zst"
        # Also write to legacy path for backward compatibility
        cp "${ARTIFACT_DIR}/${WHL_NAME}.zst" "${ARTIFACT_DIR_LEGACY}/${WHL_NAME}.zst"
        echo "  Original: $(du -sh "${whl}" | cut -f1)"
        echo "  Compressed: $(du -sh "${ARTIFACT_DIR}/${WHL_NAME}.zst" | cut -f1)"
    done

    if [ -d "${WHEEL_DIR}/requirements" ]; then
        cp -r "${WHEEL_DIR}/requirements" "${ARTIFACT_DIR}/"
        cp -r "${WHEEL_DIR}/requirements" "${ARTIFACT_DIR_LEGACY}/"
    fi
    if [ -d "${WHEEL_DIR}/tests" ]; then
        tar cf - -C "${WHEEL_DIR}" tests | zstd -9 -T0 -o "${ARTIFACT_DIR}/tests.tar.zst"
        cp "${ARTIFACT_DIR}/tests.tar.zst" "${ARTIFACT_DIR_LEGACY}/tests.tar.zst"
        echo "  Tests archive: $(du -sh "${ARTIFACT_DIR}/tests.tar.zst" | cut -f1)"
    fi

    if command -v buildkite-agent >/dev/null 2>&1; then
        buildkite-agent artifact upload "${ARTIFACT_DIR}/*"
        buildkite-agent artifact upload "${ARTIFACT_DIR_LEGACY}/*"
        echo "Wheel artifacts uploaded to ${ARTIFACT_DIR}/ and ${ARTIFACT_DIR_LEGACY}/"
    else
        echo "Not in Buildkite, skipping artifact upload"
    fi

    rm -rf "${WHEEL_DIR}"
fi
