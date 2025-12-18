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
