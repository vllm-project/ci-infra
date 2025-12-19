#!/bin/bash
set -eu -o pipefail

# Pre-warm BuildKit cache by building vLLM with cache from ECR.
# This runs as buildkite-agent user since that's who owns the buildx config.
#
# Strategy:
# 1. Clone vLLM repo
# 2. Find the latest commit that has a cache manifest in ECR
# 3. Checkout that commit and build with --cache-from
# 4. Export to --cache-to type=local for CI builds to use
#
# The ECR_TOKEN environment variable is passed from Packer.

LOCAL_CACHE_DIR="/home/buildkite-agent/.buildkit-cache"
ECR_REGISTRY="936637512419.dkr.ecr.us-east-1.amazonaws.com"
CACHE_REPO="${ECR_REGISTRY}/vllm-ci-postmerge-cache"

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

# Clone vLLM repo (full history needed to walk commits)
echo ""
echo "Cloning vLLM repository..."
VLLM_DIR="/tmp/vllm-cache-build"
rm -rf "$VLLM_DIR"
git clone --depth 50 https://github.com/vllm-project/vllm.git "$VLLM_DIR"
cd "$VLLM_DIR"

# Find the latest commit that has a cache manifest in ECR
echo ""
echo "Finding latest commit with cache in ECR..."

FOUND_COMMIT=""
for commit in $(git log --format="%H" -n 50); do
  CACHE_TAG="${CACHE_REPO}:${commit}"
  echo "Checking: ${commit:0:12}..."

  # Check if cache manifest exists for this commit
  if docker manifest inspect "$CACHE_TAG" &>/dev/null; then
    echo "✅ Found cache for commit: ${commit:0:12}"
    FOUND_COMMIT="$commit"
    break
  fi
done

if [[ -z "$FOUND_COMMIT" ]]; then
  echo "⚠️ No commit-specific cache found, falling back to 'latest'"
  CACHE_IMAGE="${CACHE_REPO}:latest"
else
  echo "Using cache from commit: $FOUND_COMMIT"
  CACHE_IMAGE="${CACHE_REPO}:${FOUND_COMMIT}"

  # Checkout the specific commit so Dockerfile matches the cache
  echo "Checking out commit ${FOUND_COMMIT:0:12}..."
  git checkout "$FOUND_COMMIT"
fi

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
