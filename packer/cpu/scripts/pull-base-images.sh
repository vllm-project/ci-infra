#!/bin/bash
set -eu -o pipefail

# Pre-pull Docker images to cache shared layers.
# Pulling the postmerge vLLM image caches all base layers it uses.
#
# NOTE: We pull from public.ecr.aws which allows unauthenticated access.
# Rate limits are lower without auth but sufficient for daily AMI builds.

echo "=== Pre-pulling Docker images ==="

# Images to pre-pull (add more as needed)
IMAGES=(
  "public.ecr.aws/q9t5s3a7/vllm-ci-postmerge-repo:latest"
)

for image in "${IMAGES[@]}"; do
  echo "Pulling: ${image}"
  sudo docker pull "${image}"
done

echo "=== Pre-pulled images ==="
sudo docker images --format "table {{.Repository}}\t{{.Tag}}\t{{.Size}}"
