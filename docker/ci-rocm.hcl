# ci-rocm.hcl - CI-specific configuration for vLLM ROCm Docker builds
#
# This file lives in ci-infra repo at docker/ci-rocm.hcl
# Used with: docker buildx bake -f docker/docker-bake-rocm.hcl -f ci-rocm.hcl test-rocm-ci
#
# Registry cache: Docker Hub (rocm/vllm-ci-cache) is used exclusively.
# AMD build agents already have Docker Hub credentials (they push the test
# image to rocm/vllm-ci), so no additional credential setup is required.
#
# sccache is disabled (USE_SCCACHE=0): AMD build agents have no AWS S3
# credentials; enabling sccache causes every HIP compilation to stall on
# S3 auth timeouts.  BuildKit's own layer cache handles stage-level caching.

# CI metadata

variable "BUILDKITE_COMMIT" {
  default = ""
}

variable "BUILDKITE_BUILD_NUMBER" {
  default = ""
}

variable "BUILDKITE_BUILD_ID" {
  default = ""
}

variable "PARENT_COMMIT" {
  default = ""
}

# Merge-base of HEAD with main — provides a more stable cache fallback than
# parent commit for long-lived PRs.  Mirrors the VLLM_MERGE_BASE_COMMIT
# pattern used in ci.hcl (CUDA).  Auto-computed by ci-bake.sh when unset.
variable "VLLM_MERGE_BASE_COMMIT" {
  default = ""
}

# Bridge to vLLM's COMMIT variable for OCI labels
variable "COMMIT" {
  default = BUILDKITE_COMMIT
}

# Image tags (set by CI)

variable "IMAGE_TAG" {
  default = ""
}

variable "IMAGE_TAG_LATEST" {
  default = ""
}

# ROCm-specific GPU architecture targets

variable "PYTORCH_ROCM_ARCH" {
  default = "gfx90a;gfx942;gfx950"
}

# Docker Hub registry cache for AMD builds.
#
# A separate repo (rocm/vllm-ci-cache) is used for BuildKit layer cache so
# that mode=max intermediate-stage blobs don't pollute the image repo.
# Docker Hub auto-creates the repo on first push.
#
# DOCKERHUB_CACHE_TO is set by the pipeline only on main-branch builds to
# keep the :rocm-latest tag warm for PR builds to pull from.

variable "DOCKERHUB_CACHE_REPO" {
  default = "rocm/vllm-ci-cache"
}

variable "DOCKERHUB_CACHE_TO" {
  default = ""
}

# Functions

function "get_cache_from_rocm" {
  params = []
  result = compact([
    # Exact commit hit — fastest cache on re-runs of the same commit
    BUILDKITE_COMMIT != "" ? "type=registry,ref=${DOCKERHUB_CACHE_REPO}:rocm-${BUILDKITE_COMMIT},mode=max" : "",
    # Parent commit — useful cache for incremental changes
    PARENT_COMMIT != "" ? "type=registry,ref=${DOCKERHUB_CACHE_REPO}:rocm-${PARENT_COMMIT},mode=max" : "",
    # Merge-base with main — stable fallback for long-lived or rebased PRs;
    # maps to a real main-branch commit whose cache layers are likely warm
    VLLM_MERGE_BASE_COMMIT != "" ? "type=registry,ref=${DOCKERHUB_CACHE_REPO}:rocm-${VLLM_MERGE_BASE_COMMIT},mode=max" : "",
    # Warm baseline — kept current by main-branch builds
    "type=registry,ref=${DOCKERHUB_CACHE_REPO}:rocm-latest,mode=max",
  ])
}

function "get_cache_to_rocm" {
  params = []
  result = compact([
    # Commit-specific tag for traceability and re-run cache hits
    BUILDKITE_COMMIT != "" ? "type=registry,ref=${DOCKERHUB_CACHE_REPO}:rocm-${BUILDKITE_COMMIT},mode=max,compression=zstd" : "",
    # rocm-latest — only set on main-branch builds (controlled by pipeline via DOCKERHUB_CACHE_TO)
    DOCKERHUB_CACHE_TO != "" ? "type=registry,ref=${DOCKERHUB_CACHE_TO},mode=max,compression=zstd" : "",
  ])
}

# CI targets

target "_ci-rocm" {
  annotations = [
    "index,manifest:vllm.buildkite.build_number=${BUILDKITE_BUILD_NUMBER}",
    "index,manifest:vllm.buildkite.build_id=${BUILDKITE_BUILD_ID}",
  ]
  args = {
    ARG_PYTORCH_ROCM_ARCH = PYTORCH_ROCM_ARCH
    USE_SCCACHE           = 0
  }
}

target "test-rocm-ci" {
  inherits   = ["_common-rocm", "_ci-rocm", "_labels"]
  target     = "test"
  cache-from = get_cache_from_rocm()
  cache-to   = get_cache_to_rocm()
  tags = compact([
    IMAGE_TAG,
    IMAGE_TAG_LATEST,
  ])
  output = ["type=registry"]
}
