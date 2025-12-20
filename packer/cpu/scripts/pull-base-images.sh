#!/bin/bash
set -eu -o pipefail

# Pre-warm BuildKit cache using regctl to copy cache from ECR.
# This runs as buildkite-agent user.
#
# Strategy:
# 1. Use regctl to copy BuildKit cache manifest from ECR to local OCI layout
# 2. CI builds use --cache-from type=local,src=/path/to/cache
#
# The ECR_TOKEN environment variable is passed from Packer.

LOCAL_CACHE_DIR="/home/buildkite-agent/.buildkit-cache"
ECR_REGISTRY="936637512419.dkr.ecr.us-east-1.amazonaws.com"
CACHE_REPO="${ECR_REGISTRY}/vllm-ci-postmerge-cache"

echo "=== Pre-warming BuildKit cache with regctl ==="
echo "Running as user: $(whoami)"

# Authenticate to ECR
if [[ -z "${ECR_TOKEN:-}" ]]; then
  echo "ERROR: ECR_TOKEN not set"
  exit 1
fi

echo "Authenticating to ECR..."
echo "$ECR_TOKEN" | regctl registry login --pass-stdin "$ECR_REGISTRY" -u AWS

# Ensure local cache directory exists
mkdir -p "$LOCAL_CACHE_DIR"

# Try to find a commit-specific cache, fall back to latest
echo ""
echo "Checking for cache in ECR..."

# First try latest
CACHE_IMAGE="${CACHE_REPO}:latest"

if regctl manifest head "$CACHE_IMAGE" &>/dev/null; then
  echo "✅ Found cache: $CACHE_IMAGE"
else
  echo "❌ No cache found at $CACHE_IMAGE"
  echo "Skipping cache warmup"
  exit 0
fi

# Copy cache from ECR to local OCI layout
echo ""
echo "Copying cache to local OCI layout..."
echo "From: ${CACHE_IMAGE}"
echo "To: ocidir://${LOCAL_CACHE_DIR}"

regctl image copy "$CACHE_IMAGE" "ocidir://${LOCAL_CACHE_DIR}"

echo ""
echo "=== BuildKit cache populated ==="
echo "Local cache location: ${LOCAL_CACHE_DIR}"
ls -la "$LOCAL_CACHE_DIR" 2>/dev/null || echo "Cannot list"
du -sh "$LOCAL_CACHE_DIR" 2>/dev/null || echo "Cannot get size"
