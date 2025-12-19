#!/bin/bash
set -eu -o pipefail

# This script runs as ec2-user via Packer provisioner.
# It installs BuildKit as a standalone systemd service for Docker builds.

echo "=== Installing BuildKit as standalone systemd service ==="

# -----------------------------------------------------------------------------
# Install BuildKit binary
# Extract buildkitd from the official moby/buildkit image
# -----------------------------------------------------------------------------
echo "=== Extracting BuildKit binary from container image ==="

# Pull the buildkit image (should already be cached)
docker pull moby/buildkit:buildx-stable-1

# Create a temporary container and copy out the binaries
CONTAINER_ID=$(docker create moby/buildkit:buildx-stable-1)
sudo docker cp "$CONTAINER_ID:/usr/bin/buildkitd" /usr/local/bin/buildkitd
sudo docker cp "$CONTAINER_ID:/usr/bin/buildctl" /usr/local/bin/buildctl
docker rm "$CONTAINER_ID"

sudo chmod +x /usr/local/bin/buildkitd /usr/local/bin/buildctl
echo "BuildKit binaries installed:"
/usr/local/bin/buildkitd --version
/usr/local/bin/buildctl --version

# -----------------------------------------------------------------------------
# Create BuildKit daemon configuration
# -----------------------------------------------------------------------------
echo "=== Creating BuildKit configuration ==="
sudo mkdir -p /etc/buildkit
sudo mkdir -p /var/lib/buildkit

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
# Create systemd service for BuildKit daemon
# -----------------------------------------------------------------------------
echo "=== Creating BuildKit systemd service ==="

cat <<'EOF' | sudo tee /etc/systemd/system/buildkitd.service
[Unit]
Description=BuildKit daemon for Docker builds
After=network.target docker.service

[Service]
Type=simple
ExecStart=/usr/local/bin/buildkitd --config /etc/buildkit/buildkitd.toml --root /var/lib/buildkit
# Set socket permissions after buildkitd creates it (make accessible to all users)
ExecStartPost=/bin/bash -c 'sleep 2 && chmod 755 /run/buildkit && chmod 666 /run/buildkit/buildkitd.sock'
Restart=always
RestartSec=5

# Security hardening
NoNewPrivileges=false
ProtectSystem=false

[Install]
WantedBy=multi-user.target
EOF

sudo chmod 644 /etc/systemd/system/buildkitd.service
sudo systemctl daemon-reload
sudo systemctl enable buildkitd.service

echo "=== Starting BuildKit daemon ==="
sudo systemctl start buildkitd.service
sleep 5  # Wait for daemon start + ExecStartPost chmod

# Verify it's running
if sudo systemctl is-active --quiet buildkitd.service; then
  echo "✅ BuildKit daemon is running"
  sudo systemctl status buildkitd.service --no-pager
else
  echo "❌ BuildKit daemon failed to start"
  sudo journalctl -u buildkitd.service --no-pager -n 50
  exit 1
fi

# Verify socket is available (use sudo since /run/buildkit is root-owned)
if sudo test -S /run/buildkit/buildkitd.sock; then
  echo "✅ BuildKit socket available at /run/buildkit/buildkitd.sock"
  sudo ls -la /run/buildkit/

  # Verify permissions allow access without sudo
  if test -r /run/buildkit/buildkitd.sock && test -w /run/buildkit/buildkitd.sock; then
    echo "✅ Socket is accessible without sudo"
  else
    echo "⚠️ Socket not accessible, manually setting permissions"
    sudo chmod 755 /run/buildkit
    sudo chmod 666 /run/buildkit/buildkitd.sock
  fi
else
  echo "❌ BuildKit socket not found"
  sudo ls -la /run/buildkit/ || echo "Directory /run/buildkit doesn't exist"
  exit 1
fi


echo "=== BuildKit installation complete ==="

# -----------------------------------------------------------------------------
# Install systemd service to create buildx config on boot
# The buildx client config (~/.docker/buildx/) doesn't persist across AMI
# snapshot/restore, so we recreate it on boot using this service.
# -----------------------------------------------------------------------------
echo "=== Installing buildx-builder systemd service ==="
sudo cp /tmp/scripts/buildx-builder.service /etc/systemd/system/buildx-builder.service
sudo chmod 644 /etc/systemd/system/buildx-builder.service
sudo systemctl daemon-reload
sudo systemctl enable buildx-builder.service
echo "Enabled buildx-builder.service to run on boot"

