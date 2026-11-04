#!/bin/bash
# The "ROCm base image cache" job of a vLLM PR build that changes the ROCm stack
# (pipeline_generator/amd.py): passes once rocm/vllm-ci has the stack image for
# ROCM_STACK_KEY, build.sh's key for the PR merged into main, so main and other
# PRs reuse it instead of rebuilding it. vLLM's `/amd-ci cache` builds that image
# and then retries this job.
# Exit 75 (retried): Docker Hub could not be asked.
set -euo pipefail

image=rocm/vllm-ci:stack-rocm-${ROCM_STACK_KEY:?}
url=https://hub.docker.com/v2/repositories/rocm/vllm-ci/tags/stack-rocm-$ROCM_STACK_KEY
code=$(curl -sS -o /dev/null -w '%{http_code}' --retry 3 --retry-connrefused "$url") || true

case $code in
  200)
    echo "$image exists"
    ;;
  404)
    msg="ROCm base image: this PR changes it, and $image does not exist yet. Once the rest of CI is green, a vLLM maintainer comments /amd-ci cache on the PR, then again once that AMD CI build passes; that retries this job. A PR behind main needs /ci run first, which merges main."
    buildkite-agent annotate --context rocm-base-image-cache --style error "$msg" || true
    echo "$msg" >&2
    exit 1
    ;;
  *)
    echo "Could not check $url (HTTP ${code:-none})" >&2
    exit 75
    ;;
esac
