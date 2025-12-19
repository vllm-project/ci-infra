#!/bin/bash
set -eu -o pipefail

# This script runs as buildkite-agent user (via sudo -u buildkite-agent -i)
# It configures buildx to use the standalone BuildKit daemon and warms the cache.

echo "=== Setting up buildx to use standalone BuildKit daemon ==="
echo "Running as user: $(whoami)"
echo "HOME directory: $HOME"

# Verify we're running as buildkite-agent
if [[ "$(whoami)" != "buildkite-agent" ]]; then
  echo "ERROR: This script must run as buildkite-agent user"
  exit 1
fi

# Verify buildkitd is running
if ! sudo systemctl is-active --quiet buildkitd.service; then
  echo "ERROR: buildkitd service is not running"
  sudo systemctl status buildkitd.service --no-pager
  exit 1
fi

# Verify socket exists
if [[ ! -S /run/buildkit/buildkitd.sock ]]; then
  echo "ERROR: BuildKit socket not found at /run/buildkit/buildkitd.sock"
  ls -la /run/buildkit/ || echo "Directory doesn't exist"
  exit 1
fi

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

# -----------------------------------------------------------------------------
# Show BuildKit cache location
# -----------------------------------------------------------------------------
echo "=== BuildKit cache location ==="
echo "Cache directory: /var/lib/buildkit"
sudo du -sh /var/lib/buildkit 2>/dev/null || echo "Could not determine size"
sudo ls -la /var/lib/buildkit/ || true

echo ""
echo "=== Summary ==="
echo "Builder name: baked-vllm-builder"
echo "Driver: remote"
echo "Endpoint: unix:///run/buildkit/buildkitd.sock"
echo "Cache: /var/lib/buildkit (persists across AMI snapshot)"
