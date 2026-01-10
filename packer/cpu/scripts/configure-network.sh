#!/bin/bash
set -eu -o pipefail

# Network tuning for high-throughput container image operations
# Optimized for r6in instances with 100Gbps networking

echo "=== Configuring network sysctl settings ==="

cat <<'EOF' | sudo tee /etc/sysctl.d/99-vllm-network.conf
# Network tuning for high-throughput Docker builds
# Reference: https://docs.aws.amazon.com/datatransferterminal/latest/userguide/tech-requirements.html

# BBR congestion control - helps sustained ECR transfers
net.core.default_qdisc = fq
net.ipv4.tcp_congestion_control = bbr

# Avoid slow start after idle - helps frequent connections
net.ipv4.tcp_slow_start_after_idle = 0

# Reasonable buffers (enough for ECR rate)
net.core.rmem_max = 16777216
net.core.wmem_max = 16777216
net.ipv4.tcp_rmem = 4096 1048576 16777216
net.ipv4.tcp_wmem = 4096 1048576 16777216
EOF

# Apply sysctl settings
sudo sysctl -p /etc/sysctl.d/99-vllm-network.conf

# -----------------------------------------------------------------------------
# Docker daemon configuration for high-throughput registry operations
# -----------------------------------------------------------------------------
echo "=== Configuring Docker daemon ==="

# Update Docker daemon config to increase concurrent downloads/uploads
# Use jq to merge with existing config, or create new if doesn't exist
if [[ -f /etc/docker/daemon.json ]]; then
    # Merge with existing config
    sudo jq '. + {"max-concurrent-downloads": 16, "max-concurrent-uploads": 16}' /etc/docker/daemon.json | sudo tee /etc/docker/daemon.json.tmp
    sudo mv /etc/docker/daemon.json.tmp /etc/docker/daemon.json
else
    # Create new config
    echo '{"max-concurrent-downloads": 16, "max-concurrent-uploads": 16}' | sudo tee /etc/docker/daemon.json
fi

# Restart Docker to apply new config
sudo systemctl restart docker
echo "Docker daemon configured and restarted"

echo "=== Network configuration complete ==="
