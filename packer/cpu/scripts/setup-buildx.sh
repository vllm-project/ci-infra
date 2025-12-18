#!/bin/bash
set -eu -o pipefail

# This script runs as buildkite-agent user (via sudo -u buildkite-agent -i)
# The -i flag ensures we get the correct HOME directory from the login shell.

echo "=== Setting up buildx builder as buildkite-agent ==="
echo "Running as user: $(whoami)"
echo "HOME directory: $HOME"

# Verify we're running as buildkite-agent
if [[ "$(whoami)" != "buildkite-agent" ]]; then
  echo "ERROR: This script must run as buildkite-agent user"
  exit 1
fi

# -----------------------------------------------------------------------------
# Create the baked builder
#
# Cache persistence strategy:
# - docker-container driver automatically creates a Docker volume for cache
# - Volume name: buildx_buildkit_<builder-name>0_state
# - This volume is stored in /var/lib/docker/volumes/ (part of EBS snapshot)
#
# After AMI snapshot/restore:
# - The volume persists (it's on the EBS volume)
# - The container is set to restart=always, so it starts automatically
# -----------------------------------------------------------------------------
echo "Creating baked-vllm-builder..."
docker buildx create \
  --name baked-vllm-builder \
  --driver docker-container \
  --driver-opt network=host \
  --config /etc/buildkit/buildkitd.toml \
  --use \
  --bootstrap

# Wait for container to be fully running
sleep 3

# Configure the builder container to restart on boot
echo "=== Configuring builder to restart on boot ==="
BUILDER_CONTAINER=$(docker ps --filter "name=buildx_buildkit_baked-vllm-builder" --format "{{.Names}}" | head -1)

if [[ -n "${BUILDER_CONTAINER}" ]]; then
  docker update --restart=always "${BUILDER_CONTAINER}"
  echo "Container '${BUILDER_CONTAINER}' configured with restart=always"
else
  echo "ERROR: Builder container not found"
  docker ps -a | grep buildkit || true
  exit 1
fi

# Verify the state volume was created
STATE_VOLUME=$(docker volume ls --format "{{.Name}}" | grep "buildx_buildkit_baked-vllm-builder" | head -1)
if [[ -z "${STATE_VOLUME}" ]]; then
  echo "ERROR: BuildKit state volume not found"
  docker volume ls
  exit 1
fi

# -----------------------------------------------------------------------------
# Verification
# -----------------------------------------------------------------------------
echo ""
echo "=== Buildx configuration ==="
echo "Config location: $HOME/.docker/buildx/"
ls -la "$HOME/.docker/buildx/"
echo ""
echo "Available builders:"
docker buildx ls
echo ""
echo "=== Summary ==="
echo "Builder name: baked-vllm-builder"
echo "Container: ${BUILDER_CONTAINER}"
echo "State volume: ${STATE_VOLUME}"
echo "Config dir: $HOME/.docker/buildx/"
