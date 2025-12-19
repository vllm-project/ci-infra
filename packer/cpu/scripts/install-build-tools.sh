#!/bin/bash
set -eu -o pipefail

# This script runs as ec2-user via Packer provisioner.
# It handles system-level setup only. User-level buildx setup is in setup-buildx.sh.

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

echo "BuildKit configuration created at /etc/buildkit/buildkitd.toml"

# -----------------------------------------------------------------------------
# Install systemd service to create buildx config on boot
#
# The buildx client config (~/.docker/buildx/) doesn't persist across AMI
# snapshot/restore. This service runs on boot to recreate it.
# -----------------------------------------------------------------------------
echo "=== Installing buildx-builder systemd service ==="
sudo cp /tmp/scripts/buildx-builder.service /etc/systemd/system/buildx-builder.service
sudo chmod 644 /etc/systemd/system/buildx-builder.service
sudo systemctl daemon-reload
sudo systemctl enable buildx-builder.service
echo "Enabled buildx-builder.service to run on boot"
