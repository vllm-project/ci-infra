#!/bin/bash
set -eu -o pipefail

# This script runs as ec2-user via Packer provisioner.
# It installs BuildKit as a standalone systemd service for Docker builds.
#
# Why standalone buildkitd instead of docker-container driver?
# - Cache persists reliably in /var/lib/buildkit across AMI snapshot/restore
# - Systemd ensures buildkitd starts on boot automatically
# - No container state to manage

echo "=== Installing BuildKit as standalone systemd service ==="

# -----------------------------------------------------------------------------
# Install BuildKit binary
# Extract buildkitd from the official moby/buildkit image
# -----------------------------------------------------------------------------
echo "=== Extracting BuildKit binary from container image ==="

# Pull the buildkit image
sudo docker pull moby/buildkit:buildx-stable-1

# Create a temporary container and copy out the binary
CONTAINER_ID=$(sudo docker create moby/buildkit:buildx-stable-1)
sudo docker cp "$CONTAINER_ID:/usr/bin/buildkitd" /usr/local/bin/buildkitd
sudo docker rm "$CONTAINER_ID"

sudo chmod +x /usr/local/bin/buildkitd
echo "BuildKit binary installed:"
/usr/local/bin/buildkitd --version

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
# max-parallelism = 32: Optimize for high-CPU build instances (r6in.16xlarge has 64 vCPUs)

[worker.oci]
  max-parallelism = 32
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
# --group docker: socket owned by docker group, accessible to buildkite-agent (who is in docker group)
ExecStart=/usr/local/bin/buildkitd --config /etc/buildkit/buildkitd.toml --root /var/lib/buildkit --group docker
Restart=always
RestartSec=5

# Prevent OOM killer from targeting buildkitd
OOMScoreAdjust=-500

# No timeout for long-running builds
TimeoutStartSec=0
TimeoutStopSec=300

# Increase file descriptor limits
LimitNOFILE=1048576

[Install]
WantedBy=multi-user.target
EOF

sudo chmod 644 /etc/systemd/system/buildkitd.service
sudo systemctl daemon-reload
sudo systemctl enable buildkitd.service

echo "=== Starting BuildKit daemon ==="
sudo systemctl start buildkitd.service

# Wait for daemon to start (up to 30 seconds)
echo "Waiting for buildkitd to start..."
for i in {1..30}; do
  if sudo systemctl is-active --quiet buildkitd.service; then
    echo "✅ BuildKit daemon is running"
    sudo systemctl status buildkitd.service --no-pager
    break
  fi
  if [[ $i -eq 30 ]]; then
    echo "❌ BuildKit daemon failed to start"
    sudo journalctl -u buildkitd.service --no-pager -n 50
    exit 1
  fi
  sleep 1
done

# Wait for socket to be created (up to 10 seconds)
echo "Waiting for BuildKit socket..."
for i in {1..10}; do
  if [[ -S /run/buildkit/buildkitd.sock ]]; then
    echo "✅ BuildKit socket available at /run/buildkit/buildkitd.sock"
    ls -la /run/buildkit/
    break
  fi
  if [[ $i -eq 10 ]]; then
    echo "❌ BuildKit socket not found after 10 seconds"
    sudo ls -la /run/buildkit/ || echo "Directory /run/buildkit doesn't exist"
    sudo journalctl -u buildkitd.service --no-pager -n 20
    exit 1
  fi
  sleep 1
done

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

# Test the service by actually starting it via systemd
# This catches systemd configuration issues before AMI creation
echo "=== Testing buildx-builder.service ==="
sudo systemctl start buildx-builder.service

# Wait for service to complete and builder to be registered (up to 10 seconds)
echo "Waiting for buildx-builder.service and builder registration..."
for i in {1..10}; do
  if sudo systemctl is-active --quiet buildx-builder.service; then
    # Service is active, check if builder is registered
    if sudo -u buildkite-agent HOME=/var/lib/buildkite-agent docker buildx ls 2>/dev/null | grep -q "^baked-vllm-builder"; then
      echo "✅ buildx-builder.service started successfully"
      echo "✅ baked-vllm-builder is configured"
      sudo -u buildkite-agent HOME=/var/lib/buildkite-agent docker buildx ls
      break
    fi
  fi
  if [[ $i -eq 10 ]]; then
    echo "❌ buildx-builder.service failed or builder not registered after 10 seconds"
    sudo systemctl status buildx-builder.service --no-pager
    echo "docker buildx ls output:"
    sudo -u buildkite-agent HOME=/var/lib/buildkite-agent docker buildx ls
    sudo journalctl -u buildx-builder.service --no-pager -n 50
    exit 1
  fi
  sleep 1
done

echo "=== BuildKit installation complete ==="
