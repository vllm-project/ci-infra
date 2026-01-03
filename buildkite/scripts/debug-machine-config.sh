#!/bin/bash
# debug-machine-config.sh - Print machine configuration for Docker builds

echo "=== MACHINE CONFIG ==="

# Get IMDSv2 token
TOKEN=$(curl -s -X PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 21600" 2>/dev/null) || true
INSTANCE_ID=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/instance-id 2>/dev/null || echo 'N/A')
INSTANCE_TYPE=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/instance-type 2>/dev/null || echo 'N/A')

echo "Instance: $INSTANCE_ID ($INSTANCE_TYPE)"
echo "CPUs: $(nproc), Memory: $(free -h | awk '/^Mem:/{print $2}')"

echo ""
echo "--- Network ---"
sysctl -n net.ipv4.tcp_congestion_control 2>/dev/null | xargs -I{} echo "TCP congestion: {}"
sysctl -n net.core.rmem_max 2>/dev/null | xargs -I{} echo "Buffer max: {} bytes"

echo ""
echo "--- Docker ---"
cat /etc/docker/daemon.json 2>/dev/null || echo "No daemon.json"

echo ""
echo "--- BuildKit ---"
cat /etc/buildkit/buildkitd.toml 2>/dev/null | grep -v "^#" | grep -v "^$" || echo "No buildkitd.toml"

echo ""
echo "--- Disk ---"
df -h / /var/lib/docker 2>/dev/null | tail -n +2

echo "=== END ==="
