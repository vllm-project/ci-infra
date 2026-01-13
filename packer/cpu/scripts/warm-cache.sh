#!/bin/bash
set -eu -o pipefail

# Warm the Docker cache by building the vLLM image.
# This runs "docker buildx bake cache-warm" which:
# - Pulls cache from postmerge-cache:latest and specified commit
# - Builds the image (caching layers locally in /var/lib/buildkit)
# - Outputs to cache only (no image pushed)
#
# Files uploaded by Packer:
# - /tmp/ci.hcl - CI configuration from ci-infra
# - /tmp/vllm - vLLM repo (cloned by pipeline, uploaded by Packer)
#
# Environment variables:
# - ECR_TOKEN: Required for ECR authentication
# - VLLM_COMMIT: Commit to build/cache from (for cache key parity with CI)
#
# The ECR_TOKEN environment variable is passed from Packer for auth.

echo "=== Warming Docker cache with vLLM build ==="
echo "Running as user: $(whoami)"

# ECR authentication (passed from Packer)
if [[ -z "${ECR_TOKEN:-}" ]]; then
  echo "ERROR: ECR_TOKEN not set"
  exit 1
fi

ECR_REGISTRY="936637512419.dkr.ecr.us-east-1.amazonaws.com"
echo "Authenticating to ECR..."
echo "$ECR_TOKEN" | docker login --password-stdin --username AWS "$ECR_REGISTRY"

# Check for required files (uploaded by Packer)
CI_HCL_PATH="/tmp/ci.hcl"
VLLM_DIR="/tmp/vllm"

if [[ ! -f "${CI_HCL_PATH}" ]]; then
  echo "ERROR: ci.hcl not found at ${CI_HCL_PATH}"
  exit 1
fi

if [[ ! -d "${VLLM_DIR}" ]]; then
  echo "ERROR: vLLM directory not found at ${VLLM_DIR}"
  echo "The pipeline should have cloned vLLM and Packer should have uploaded it"
  exit 1
fi

# Check if docker-bake.hcl exists
if [[ ! -f "${VLLM_DIR}/docker/docker-bake.hcl" ]]; then
  echo "WARNING: docker/docker-bake.hcl not found in vLLM repo"
  echo "Cache warming requires docker-bake.hcl to be present"
  echo "Skipping cache warming - AMI will still work but without warm cache"
  exit 0
fi

echo "Using ci.hcl from ${CI_HCL_PATH}"
echo "Using vLLM from ${VLLM_DIR}"

cd "${VLLM_DIR}"

# Use the baked builder that was created in setup-buildx.sh
echo "Using baked-vllm-builder (connected to buildkitd)..."
docker buildx use baked-vllm-builder

# Print resolved config
echo "=== Resolved bake configuration ==="
BUILDKITE_COMMIT="${VLLM_COMMIT:-}" \
VLLM_USE_PRECOMPILED="${VLLM_USE_PRECOMPILED:-1}" \
VLLM_MERGE_BASE_COMMIT="${VLLM_MERGE_BASE_COMMIT:-}" \
docker buildx bake -f docker/docker-bake.hcl -f "${CI_HCL_PATH}" --print cache-warm || true

# Run cache-warm build
echo "=== Running cache-warm build ==="
echo "This will populate /var/lib/buildkit with cached layers..."
if [[ -n "${VLLM_COMMIT:-}" ]]; then
  echo "Building from commit: ${VLLM_COMMIT}"
  echo "Setting BUILDKITE_COMMIT=${VLLM_COMMIT} for cache key parity with CI"
fi
echo "VLLM_USE_PRECOMPILED=${VLLM_USE_PRECOMPILED:-1}"
echo "VLLM_MERGE_BASE_COMMIT=${VLLM_MERGE_BASE_COMMIT:-}"
BUILDKITE_COMMIT="${VLLM_COMMIT:-}" \
VLLM_USE_PRECOMPILED="${VLLM_USE_PRECOMPILED:-1}" \
VLLM_MERGE_BASE_COMMIT="${VLLM_MERGE_BASE_COMMIT:-}" \
docker buildx bake -f docker/docker-bake.hcl -f "${CI_HCL_PATH}" --progress plain cache-warm || {
  echo "Build completed (cache warming done even if build had warnings)"
}

# Show cache status
echo ""
echo "=== BuildKit cache status ==="
echo "BuildKit storage:"
du -sh /var/lib/buildkit 2>/dev/null || echo "Cannot access /var/lib/buildkit without sudo"

echo ""
echo "=== Cache warming complete ==="
