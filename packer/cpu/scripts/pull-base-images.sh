#!/bin/bash
set -eu -o pipefail

# Pre-pull Docker cache images into BuildKit cache.
# This runs as buildkite-agent user since that's who owns the buildx config.
#
# We use buildx to pull images so they're cached in /var/lib/buildkit,
# not in Docker's layer store (/var/lib/docker).
#
# The ECR_TOKEN environment variable is passed from Packer.

echo "=== Pre-pulling cache images into BuildKit cache ==="
echo "Running as user: $(whoami)"

# Authenticate to ECR using token passed from Packer
ECR_REGISTRY="936637512419.dkr.ecr.us-east-1.amazonaws.com"

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

# Cache image to pre-pull (this is what --cache-from uses in CI builds)
CACHE_IMAGE="${ECR_REGISTRY}/vllm-ci-postmerge-cache:latest"

echo ""
echo "Pulling cache image into BuildKit cache: ${CACHE_IMAGE}"

# Pull the cache image using buildx with --load to force all layers to download
echo "FROM ${CACHE_IMAGE}" | docker buildx build --builder baked-vllm-builder --load --progress plain -

echo ""
echo "=== BuildKit cache populated with cache image ==="
echo "Cache location: /var/lib/buildkit"
ls -la /var/lib/buildkit/ 2>/dev/null || echo "Cannot list (permission denied)"
du -sh /var/lib/buildkit 2>/dev/null || echo "Cannot get size"
