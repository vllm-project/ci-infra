#!/bin/bash
set -eu -o pipefail

# This script warms the BuildKit cache by running a build.
# It runs as buildkite-agent user since that's who owns the buildx config.
# NOTE: This script cannot use sudo - buildkite-agent has no sudo access.

echo "=== Warming BuildKit cache ==="
echo "Running as user: $(whoami)"
echo "HOME directory: $HOME"

# Verify we're running as buildkite-agent
if [[ "$(whoami)" != "buildkite-agent" ]]; then
  echo "ERROR: This script must run as buildkite-agent user"
  exit 1
fi

# Verify buildx builder is configured
if ! docker buildx inspect baked-vllm-builder &>/dev/null; then
  echo "ERROR: baked-vllm-builder not found"
  docker buildx ls
  exit 1
fi

docker buildx use baked-vllm-builder
echo "Using builder: baked-vllm-builder"

# Clone vllm repo to get Dockerfile
WORK_DIR=$(mktemp -d)
cd "$WORK_DIR"

echo ""
echo "=== Cloning vllm repository ==="
git clone --depth 1 https://github.com/vllm-project/vllm.git .

echo ""
echo "=== Running build to warm cache ==="
echo "Building from scratch to populate local BuildKit cache."
echo "This may take 30-60 minutes but will save time on every CI build."

# Run the build to populate buildkit cache
# Don't need --cache-from or --cache-to or --push - just build to cache locally
# Use simple args to avoid errors
docker buildx build \
  --file docker/Dockerfile \
  --build-arg max_jobs=8 \
  --build-arg USE_SCCACHE=1 \
  --target test \
  --progress plain \
  . || {
    echo "Build failed but may have partial cache"
    echo "Continuing with whatever cache was populated..."
  }

# Cleanup
cd /
rm -rf "$WORK_DIR"

echo ""
echo "=== BuildKit cache warmed ==="
echo "Cache location: /var/lib/buildkit"
ls -la /var/lib/buildkit/ 2>/dev/null || echo "Cannot list cache directory"
