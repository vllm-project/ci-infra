#!/bin/bash
set -eu -o pipefail

# Pre-pull Docker images into BuildKit cache.
# This runs as buildkite-agent user since that's who owns the buildx config.
#
# We use buildx to pull images so they're cached in /var/lib/buildkit,
# not in Docker's layer store (/var/lib/docker).
#
# NOTE: We pull from public.ecr.aws which allows unauthenticated access.

echo "=== Pre-pulling images into BuildKit cache ==="
echo "Running as user: $(whoami)"

# Verify buildx builder is configured
if ! docker buildx inspect baked-vllm-builder &>/dev/null; then
  echo "ERROR: baked-vllm-builder not found"
  docker buildx ls
  exit 1
fi

docker buildx use baked-vllm-builder
echo "Using builder: baked-vllm-builder"

# Images to pre-pull (add more as needed)
IMAGES=(
  "public.ecr.aws/q9t5s3a7/vllm-ci-postmerge-repo:latest"
)

for image in "${IMAGES[@]}"; do
  echo ""
  echo "Pulling into BuildKit cache: ${image}"
  # Build a minimal Dockerfile that just pulls the image
  # This caches all layers in BuildKit's cache (/var/lib/buildkit)
  echo "FROM ${image}" | docker buildx build --builder baked-vllm-builder --progress plain -
done

echo ""
echo "=== BuildKit cache populated ==="
echo "Cache location: /var/lib/buildkit"
ls -la /var/lib/buildkit/ 2>/dev/null || echo "Cannot list (permission denied)"
