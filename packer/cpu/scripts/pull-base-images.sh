#!/bin/bash
set -eu -o pipefail

# Pre-pull Docker images to cache shared layers.
# Pulling the postmerge vLLM image caches all base layers it uses.
#
# NOTE: We may consider running an actual build as part of the AMI creation
# to warm the BuildKit cache with compiled layers. However, this simple
# approach of just pulling images is the easiest start and doesn't require
# the complex build steps (cloning repo, setting up build args, etc.).

echo "=== Pre-pulling Docker images ==="

# Login to ECR Public (required to pull from public.ecr.aws)
echo "=== Logging into ECR Public ==="
aws ecr-public get-login-password --region us-east-1 | sudo docker login --username AWS --password-stdin public.ecr.aws

# Images to pre-pull (add more as needed)
IMAGES=(
  "public.ecr.aws/q9t5s3a7/vllm-ci-postmerge-repo:latest"
)

for image in "${IMAGES[@]}"; do
  echo "Pulling: ${image}"
  sudo docker pull "${image}" || true
done

echo "=== Pre-pulled images ==="
sudo docker images --format "table {{.Repository}}\t{{.Tag}}\t{{.Size}}"
