#!/bin/bash
set -eu -o pipefail

# This script runs as ec2-user, so we need sudo for system changes

echo "=== Installing build tools and optimizing Docker for CI builds ==="

# -----------------------------------------------------------------------------
# Create BuildKit daemon configuration
# -----------------------------------------------------------------------------
echo "=== Creating BuildKit configuration ==="
sudo mkdir -p /etc/buildkit
cat <<'EOF' | sudo tee /etc/buildkit/buildkitd.toml
# BuildKit daemon configuration for vLLM CI builds
#
# gc = false: Garbage collection disabled because:
#   - Instances are ephemeral (terminated after each job)
#   - AMI is rebuilt daily with fresh cache
#   - 512GB disk is more than enough for a single build
#
# max-parallelism = 16: Optimize for high-CPU build instances

[worker.oci]
  max-parallelism = 16
  gc = false

EOF

# -----------------------------------------------------------------------------
# Create the baked builder
#
# NOTE: Prefixed with "baked-" to avoid conflict with CI's "vllm-builder".
#
# Cache persistence strategy (from Docker docs):
# - docker-container driver automatically creates a Docker volume for cache
# - Volume name: buildx_buildkit_<builder-name>0_state
# - This volume is stored in /var/lib/docker/volumes/ (part of EBS snapshot)
#
# After AMI snapshot/restore:
# - The volume persists (it's on the EBS volume)
# - The container is set to restart=always, so it starts automatically
# - If the container is deleted, CI can recreate the builder with the same
#   name to inherit the existing volume and cached state
# -----------------------------------------------------------------------------
echo "=== Creating baked builder ==="

# network=host: Avoids Docker network overhead, enables direct ECR/registry access
sudo docker buildx create \
  --name baked-vllm-builder \
  --driver docker-container \
  --driver-opt network=host \
  --config /etc/buildkit/buildkitd.toml \
  --use \
  --bootstrap

# Show the created volume
echo "=== BuildKit state volume created ==="
sudo docker volume ls | grep buildkit || true

# -----------------------------------------------------------------------------
# Configure the builder container to restart on boot
# -----------------------------------------------------------------------------
echo "=== Configuring builder to restart on boot ==="

# Wait for container to be fully running
sleep 3

# Dynamically get the builder container name (don't hardcode it)
BUILDER_CONTAINER=$(sudo docker ps --filter "name=buildx_buildkit_baked-vllm-builder" --format "{{.Names}}" | head -1)

if [[ -n "${BUILDER_CONTAINER}" ]]; then
  sudo docker update --restart=always "${BUILDER_CONTAINER}"
  echo "=== Builder container '${BUILDER_CONTAINER}' configured with restart=always ==="
else
  echo "ERROR: Builder container not found"
  sudo docker ps -a | grep buildkit || true
  exit 1
fi

# Get the state volume name
STATE_VOLUME=$(sudo docker volume ls --format "{{.Name}}" | grep "buildx_buildkit_baked-vllm-builder" | head -1)

if [[ -z "${STATE_VOLUME}" ]]; then
  echo "ERROR: BuildKit state volume not found"
  sudo docker volume ls
  exit 1
fi

# Verify setup
echo "=== BuildKit setup complete ==="
sudo docker buildx ls
sudo docker ps --filter "name=buildkit" --format "table {{.Names}}\t{{.Status}}\t{{.Image}}"
sudo docker volume ls | grep buildkit

echo ""
echo "=== Configuration summary ==="
echo "BuildKit config: /etc/buildkit/buildkitd.toml"
echo "Builder name: baked-vllm-builder"
echo "Builder container: ${BUILDER_CONTAINER:-not found}"
echo "State volume: ${STATE_VOLUME:-not found}"
echo ""
echo "CI can recreate builder with same name to inherit cache:"
echo "  docker buildx create --name baked-vllm-builder --driver docker-container --config /etc/buildkit/buildkitd.toml --use --bootstrap"
