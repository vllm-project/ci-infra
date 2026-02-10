#!/bin/bash
set -eu -o pipefail

# Warm the Docker cache by building the vLLM image.
# This runs "docker buildx bake cache-warm" which:
# - Pulls cache from the commit-specific cache registry
# - Builds the image (caching layers locally in /var/lib/buildkit)
# - Outputs to cache only (no image pushed)
#
# Files uploaded by Packer:
# - /tmp/ci-bake-config.json - CI build configuration artifact
# - /tmp/cache-warm-overlay.hcl - Overlay that defines cache-warm target
# - /tmp/vllm - vLLM repo (cloned by pipeline, uploaded by Packer)
#
# Environment variables:
# - ECR_TOKEN: Required for ECR authentication
# - VLLM_COMMIT: Commit to build/cache from (for cache key parity with CI)

echo "=== Warming Docker cache with vLLM build ==="
echo "Running as user: $(whoami)"

# ECR authentication (passed from Packer)
if [[ -z "${ECR_TOKEN:-}" ]]; then
  echo "ERROR: ECR_TOKEN not set"
  exit 1
fi

if [[ -z "${VLLM_COMMIT:-}" ]]; then
  echo "ERROR: VLLM_COMMIT not set"
  exit 1
fi

ECR_REGISTRY="936637512419.dkr.ecr.us-east-1.amazonaws.com"
echo "Authenticating to ECR..."
echo "$ECR_TOKEN" | docker login --password-stdin --username AWS "$ECR_REGISTRY"

# Check for required files (uploaded by Packer)
VLLM_DIR="/tmp/vllm"
BAKE_CONFIG="/tmp/ci-bake-config.json"
OVERLAY_CONFIG="/tmp/cache-warm-overlay.hcl"

if [[ ! -d "${VLLM_DIR}" ]]; then
  echo "ERROR: vLLM directory not found at ${VLLM_DIR}"
  echo "The pipeline should have cloned vLLM and Packer should have uploaded it"
  exit 1
fi

if [[ ! -f "${BAKE_CONFIG}" ]]; then
  echo "ERROR: Bake config not found at ${BAKE_CONFIG}"
  echo "The AMI pipeline should have downloaded the bake config artifact"
  exit 1
fi

if [[ ! -f "${OVERLAY_CONFIG}" ]]; then
  echo "ERROR: Overlay config not found at ${OVERLAY_CONFIG}"
  exit 1
fi

echo "Using vLLM from ${VLLM_DIR}"
echo "Using bake config from ${BAKE_CONFIG}"
echo "Using overlay from ${OVERLAY_CONFIG}"
echo "Cache commit: ${VLLM_COMMIT}"

cd "${VLLM_DIR}"

# Use the baked builder that was created in setup-buildx.sh
echo "Using baked-vllm-builder (connected to buildkitd)..."
docker buildx use baked-vllm-builder

# Print resolved config
echo "=== Resolved bake configuration ==="
docker buildx bake \
  -f "${BAKE_CONFIG}" \
  -f "${OVERLAY_CONFIG}" \
  --set "cache-warm.cache-from=type=registry,ref=${ECR_REGISTRY}/vllm-ci-test-cache:${VLLM_COMMIT},mode=max" \
  --print cache-warm || true

# Run cache-warm build
echo "=== Running cache-warm build ==="
echo "This will populate /var/lib/buildkit with cached layers..."
echo "Pulling cache from: ${ECR_REGISTRY}/vllm-ci-test-cache:${VLLM_COMMIT}"
docker buildx bake \
  -f "${BAKE_CONFIG}" \
  -f "${OVERLAY_CONFIG}" \
  --set "cache-warm.cache-from=type=registry,ref=${ECR_REGISTRY}/vllm-ci-test-cache:${VLLM_COMMIT},mode=max" \
  --progress plain cache-warm

# Show cache status
echo ""
echo "=== BuildKit cache status ==="
echo "BuildKit storage:"
du -sh /var/lib/buildkit 2>/dev/null || echo "Cannot access /var/lib/buildkit without sudo"

echo ""
echo "=== Cache warming complete ==="
