#!/bin/bash
set -eu -o pipefail

# Pre-warm BuildKit cache by building vLLM with cache from ECR.
# This runs as buildkite-agent user since that's who owns the buildx config.
#
# Strategy:
# 1. Clone vLLM repo to get the Dockerfile
# 2. Build with --cache-from to pull cache from ECR registry
# 3. Export to --cache-to type=local for CI builds to use
#
# The ECR_TOKEN environment variable is passed from Packer.

LOCAL_CACHE_DIR="/var/lib/buildkit/vllm-cache"
ECR_REGISTRY="936637512419.dkr.ecr.us-east-1.amazonaws.com"
CACHE_IMAGE="${ECR_REGISTRY}/vllm-ci-postmerge-cache:latest"

echo "=== Pre-warming BuildKit cache ==="
echo "Running as user: $(whoami)"

# Authenticate to ECR
if [[ -z "${ECR_TOKEN:-}" ]]; then
  echo "ERROR: ECR_TOKEN not set"
  exit 1
fi

echo "Authenticating to ECR..."
echo "$ECR_TOKEN" | docker login --username AWS --password-stdin "$ECR_REGISTRY"

# Verify buildx builder is configured
if ! docker buildx inspect baked-vllm-builder &>/dev/null; then
  echo "ERROR: baked-vllm-builder not found"
  docker buildx ls
  exit 1
fi

docker buildx use baked-vllm-builder
echo "Using builder: baked-vllm-builder"

# Clone vLLM repo
echo ""
echo "Cloning vLLM repository..."
VLLM_DIR="/tmp/vllm-cache-build"
rm -rf "$VLLM_DIR"
git clone --depth 1 https://github.com/vllm-project/vllm.git "$VLLM_DIR"
cd "$VLLM_DIR"

echo ""
echo "Building with cache-from registry, cache-to local..."
echo "Cache from: ${CACHE_IMAGE}"
echo "Cache to: ${LOCAL_CACHE_DIR}"

# Build with cache import/export
# Use same build args as CI to match cache keys
docker buildx build \
  --builder baked-vllm-builder \
  --file docker/Dockerfile \
  --build-arg max_jobs=16 \
  --build-arg USE_SCCACHE=1 \
  --build-arg TORCH_CUDA_ARCH_LIST="8.0 8.9 9.0 10.0" \
  --build-arg FI_TORCH_CUDA_ARCH_LIST="8.0 8.9 9.0a 10.0a" \
  --build-arg VLLM_USE_PRECOMPILED=0 \
  --cache-from "type=registry,ref=${CACHE_IMAGE}" \
  --cache-to "type=local,dest=${LOCAL_CACHE_DIR},mode=max" \
  --target test \
  --progress plain \
  . || echo "Build stopped (expected - we just want the cache)"

# Cleanup
cd /
rm -rf "$VLLM_DIR"

# Logout from ECR
echo ""
echo "Logging out from ECR..."
docker logout "$ECR_REGISTRY"

echo ""
echo "=== BuildKit cache populated ==="
echo "Local cache location: ${LOCAL_CACHE_DIR}"
ls -la "$LOCAL_CACHE_DIR" 2>/dev/null || echo "Cannot list (permission denied)"
du -sh "$LOCAL_CACHE_DIR" 2>/dev/null || echo "Cannot get size"
echo ""
echo "BuildKit cache location: /var/lib/buildkit"
du -sh /var/lib/buildkit 2>/dev/null || echo "Cannot get size"
