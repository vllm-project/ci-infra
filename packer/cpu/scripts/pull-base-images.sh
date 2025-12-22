#!/bin/bash
set -eu -o pipefail

# Pre-warm BuildKit cache by running a build with --cache-from registry.
# This extracts all cached layers into /var/lib/buildkit in BuildKit's internal format.
#
# Strategy:
# 1. Clone vLLM repo
# 2. Run build with --cache-from type=registry to fetch and extract cache
# 3. BuildKit populates /var/lib/buildkit with extracted snapshots
# 4. AMI snapshot captures the pre-populated cache
#
# The ECR_TOKEN environment variable is passed from Packer.

ECR_REGISTRY="936637512419.dkr.ecr.us-east-1.amazonaws.com"
CACHE_REPO="${ECR_REGISTRY}/vllm-ci-postmerge-cache"
WORK_DIR="/tmp/vllm-cache-warmup"

echo "=== Pre-warming BuildKit cache ==="
echo "Running as user: $(whoami)"
echo "Work directory: ${WORK_DIR}"

# Authenticate to ECR
if [[ -z "${ECR_TOKEN:-}" ]]; then
  echo "ERROR: ECR_TOKEN not set"
  exit 1
fi

echo "Authenticating to ECR..."
echo "$ECR_TOKEN" | docker login --password-stdin --username AWS "$ECR_REGISTRY"

# Check if cache exists
CACHE_IMAGE="${CACHE_REPO}:latest"
echo ""
echo "Checking for cache in ECR..."

# Use docker manifest to check if cache exists
if ! docker manifest inspect "$CACHE_IMAGE" &>/dev/null; then
  echo "❌ No cache found at $CACHE_IMAGE"
  echo "Skipping cache warmup - will build from scratch on first CI run"
  exit 0
fi

echo "✅ Found cache: $CACHE_IMAGE"

# Clone vLLM repo
echo ""
echo "Cloning vLLM repository..."
rm -rf "$WORK_DIR"
mkdir -p "$WORK_DIR"
git clone --depth 1 https://github.com/vllm-project/vllm.git "$WORK_DIR"
cd "$WORK_DIR"

# Create buildx builder connected to local buildkitd
echo ""
echo "Setting up buildx builder..."
docker buildx rm cache-warmer 2>/dev/null || true

if [[ -S /run/buildkit/buildkitd.sock ]]; then
  echo "Connecting to local buildkitd..."
  docker buildx create --name cache-warmer --driver remote --use unix:///run/buildkit/buildkitd.sock
else
  echo "ERROR: buildkitd socket not found at /run/buildkit/buildkitd.sock"
  exit 1
fi

docker buildx inspect --bootstrap

# Run build with cache-from to populate local cache
# This is the key step - BuildKit will fetch and extract all cached layers
echo ""
echo "Running build to populate cache..."
echo "This will download and extract cached layers into /var/lib/buildkit"
echo ""

# Match the exact build args used in CI for cache key compatibility
docker buildx build \
  --cache-from type=registry,ref=${CACHE_IMAGE},mode=max \
  --build-arg max_jobs=16 \
  --build-arg USE_SCCACHE=1 \
  --build-arg TORCH_CUDA_ARCH_LIST="8.0 8.9 9.0 10.0" \
  --build-arg FI_TORCH_CUDA_ARCH_LIST="8.0 8.9 9.0a 10.0a" \
  --target test \
  -f docker/Dockerfile \
  --load \
  --progress plain \
  . 2>&1 | tee /tmp/cache-warmup.log || {
    echo "Build failed or completed with warnings - checking cache status..."
  }

# Show final cache status
echo ""
echo "=== BuildKit cache status ==="
echo "BuildKit storage:"
sudo du -sh /var/lib/buildkit 2>/dev/null || echo "Cannot access /var/lib/buildkit"
sudo ls -la /var/lib/buildkit/ 2>/dev/null || echo "Cannot list"

echo ""
echo "Content store:"
sudo du -sh /var/lib/buildkit/content 2>/dev/null || echo "No content store yet"

echo ""
echo "Snapshots:"
sudo ls -la /var/lib/buildkit/runc-overlayfs/snapshots/ 2>/dev/null | head -20 || echo "No snapshots yet"

# Cleanup
echo ""
echo "Cleaning up work directory..."
cd /
rm -rf "$WORK_DIR"
docker buildx rm cache-warmer 2>/dev/null || true

echo ""
echo "=== Cache warmup complete ==="
