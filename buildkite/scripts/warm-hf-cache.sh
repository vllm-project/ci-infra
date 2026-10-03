#!/bin/bash
set -eu -o pipefail

# Warm the shared HuggingFace cache used by GPU test steps.
#
# Test containers on the L4 GPU queues get HF_HOME=/fsx/hf_cache mounted from
# the host (buildkite/pipeline_generator/plugin/docker_plugin.py), so this
# script runs the *same* CI test image with the same mount and pre-downloads
# the small models/datasets behind the most download-flaky test groups
# (bitsandbytes plugins, MTEB pooling). A nightly run keeps the cache warm so
# those steps never touch the network; combined with HF_HUB_OFFLINE=1 on the
# vllm side this removes the HF Hub outage failure class.
#
# Environment variables:
# - HF_CACHE_HOST_PATH: host path of the shared cache (default /fsx/hf_cache)
# - WARM_IMAGE: image to warm from (default: latest postmerge test image, so
#   the cache layout is produced by the same library versions tests use)
# - WARM_MODELS / WARM_MTEB / WARM_GSM8K_MODELS: see warm_hf_cache.py
# - HF_TOKEN: optional; all default repos are ungated

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

HF_CACHE_HOST_PATH="${HF_CACHE_HOST_PATH:-/fsx/hf_cache}"
WARM_IMAGE="${WARM_IMAGE:-public.ecr.aws/q9t5s3a7/vllm-ci-postmerge-repo:latest}"

echo "=== Warming HF cache at ${HF_CACHE_HOST_PATH} ==="
echo "Warm image: ${WARM_IMAGE}"

if [[ ! -d "${HF_CACHE_HOST_PATH}" ]]; then
  echo "ERROR: ${HF_CACHE_HOST_PATH} not found on this host."
  echo "This script must run on a queue whose agents mount the shared FSx"
  echo "cache (e.g. gpu_1_queue); see plugin/docker_plugin.py."
  exit 1
fi

echo "Pulling warm image..."
docker pull "${WARM_IMAGE}"

DOCKER_ARGS=(
  --rm
  -v "${HF_CACHE_HOST_PATH}:${HF_CACHE_HOST_PATH}"
  -v "${SCRIPT_DIR}/warm_hf_cache.py:/tmp/warm_hf_cache.py:ro"
  -e "HF_HOME=${HF_CACHE_HOST_PATH}"
  -e "HF_HUB_ENABLE_HF_TRANSFER=1"
)
# Pass through the optional knobs only when set, so warm_hf_cache.py defaults
# apply otherwise.
for var in WARM_MODELS WARM_MTEB WARM_GSM8K_MODELS HF_TOKEN; do
  if [[ -n "${!var:-}" ]]; then
    DOCKER_ARGS+=(-e "${var}=${!var}")
  fi
done

docker run "${DOCKER_ARGS[@]}" "${WARM_IMAGE}" python3 /tmp/warm_hf_cache.py

echo "=== HF cache warm complete ==="
