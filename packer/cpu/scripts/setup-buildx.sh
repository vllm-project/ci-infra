#!/bin/bash
set -eu -o pipefail

# This script runs as buildkite-agent user (via sudo -u buildkite-agent -i)
# It configures buildx to use the standalone BuildKit daemon.
# NOTE: This script cannot use sudo - buildkite-agent has no sudo access.

echo "=== Setting up buildx to use standalone BuildKit daemon ==="
echo "Running as user: $(whoami)"
echo "HOME directory: $HOME"

# Verify we're running as buildkite-agent
if [[ "$(whoami)" != "buildkite-agent" ]]; then
  echo "ERROR: This script must run as buildkite-agent user"
  exit 1
fi

# Verify socket exists and is accessible
# (install-build-tools.sh should have set permissions via ExecStartPost)
if [[ ! -S /run/buildkit/buildkitd.sock ]]; then
  echo "ERROR: BuildKit socket not found at /run/buildkit/buildkitd.sock"
  ls -la /run/buildkit/ 2>/dev/null || echo "Directory /run/buildkit doesn't exist or not accessible"
  exit 1
fi

# Verify we can access the socket
if [[ ! -r /run/buildkit/buildkitd.sock ]] || [[ ! -w /run/buildkit/buildkitd.sock ]]; then
  echo "ERROR: BuildKit socket exists but is not readable/writable"
  ls -la /run/buildkit/buildkitd.sock
  exit 1
fi

echo "✅ BuildKit socket is accessible at /run/buildkit/buildkitd.sock"

# -----------------------------------------------------------------------------
# Create buildx builder using remote driver
# This connects to the standalone buildkitd daemon via Unix socket
# -----------------------------------------------------------------------------
echo "Creating baked-vllm-builder using remote driver..."

# Remove any existing builder with this name
docker buildx rm baked-vllm-builder 2>/dev/null || true

docker buildx create \
  --name baked-vllm-builder \
  --driver remote \
  --use \
  unix:///run/buildkit/buildkitd.sock

echo "✅ Builder created"

# Verify the builder works
echo ""
echo "=== Buildx configuration ==="
docker buildx inspect baked-vllm-builder
echo ""
echo "Available builders:"
docker buildx ls
echo ""

echo "=== Summary ==="
echo "Builder name: baked-vllm-builder"
echo "Driver: remote"
echo "Endpoint: unix:///run/buildkit/buildkitd.sock"
echo "Cache: /var/lib/buildkit (persists across AMI snapshot)"
