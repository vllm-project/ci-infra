# ci-rocm.hcl - CI-specific configuration for vLLM ROCm Docker builds
#
# This file lives in ci-infra repo at docker/ci-rocm.hcl
# Used with: docker buildx bake -f docker/docker-bake-rocm.hcl -f ci-rocm.hcl test-rocm-ci
#
# Registry cache: Docker Hub (rocm/vllm-ci-cache) is used exclusively.
# AMD build agents already have Docker Hub credentials (they push the test
# image to rocm/vllm-ci), so no additional credential setup is required.
#
# sccache is enabled for ROCm CI builds. AMD build agents receive AWS
# credentials from the runner pod, and buildx forwards them into the Docker
# build as BuildKit secrets so compiler cache hits can be shared through S3
# without baking credentials into layers. BuildKit's own registry cache still
# handles stage-level reuse; sccache only accelerates work inside re-executed
# compile layers.

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

# Pre-built CI base image (Tier 1). Per-PR builds pull this instead of
# rebuilding RIXL/DeepEP/torchcodec from scratch. The ci_base stage in
# Dockerfile.rocm inherits from base, so CI_BASE_IMAGE only affects the test
# stage and is irrelevant when building --target ci_base itself.
variable "CI_BASE_IMAGE" {
  default = "rocm/vllm-dev:ci_base"
}

# sccache configuration

variable "USE_SCCACHE" {
  default = 1
}

variable "SCCACHE_BUCKET_NAME" {
  default = "vllm-build-sccache"
}

variable "SCCACHE_REGION_NAME" {
  default = "us-west-2"
}

variable "SCCACHE_S3_NO_CREDENTIALS" {
  default = 0
}

# Docker Hub registry cache for AMD builds.
#
# A separate repo (rocm/vllm-ci-cache) is used for BuildKit layer cache.
# cache-to uses mode=min (final layers only) to avoid pushing the large
# rocm/vllm-dev:base layers (~7.25GB ROCm runtime) which cause Docker Hub
# upload session timeouts (400 Bad Request). The base layers are already
# available as a separate image on Docker Hub.
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
    BUILDKITE_COMMIT != "" ? "type=registry,ref=${DOCKERHUB_CACHE_REPO}:rocm-${BUILDKITE_COMMIT},mode=min,compression=zstd" : "",
    # rocm-latest — only set on main-branch builds (controlled by pipeline via DOCKERHUB_CACHE_TO)
    DOCKERHUB_CACHE_TO != "" ? "type=registry,ref=${DOCKERHUB_CACHE_TO},mode=min,compression=zstd" : "",
  ])
}

# CI targets

target "_ci-rocm" {
  annotations = [
    "manifest:vllm.buildkite.build_number=${BUILDKITE_BUILD_NUMBER}",
    "manifest:vllm.buildkite.build_id=${BUILDKITE_BUILD_ID}",
  ]
  secret = [
    "id=aws_access_key_id,env=AWS_ACCESS_KEY_ID",
    "id=aws_secret_access_key,env=AWS_SECRET_ACCESS_KEY",
  ]
  args = {
    ARG_PYTORCH_ROCM_ARCH = PYTORCH_ROCM_ARCH
    USE_SCCACHE           = USE_SCCACHE
    SCCACHE_BUCKET_NAME   = SCCACHE_BUCKET_NAME
    SCCACHE_REGION_NAME   = SCCACHE_REGION_NAME
    SCCACHE_S3_NO_CREDENTIALS = SCCACHE_S3_NO_CREDENTIALS
    CI_BASE_IMAGE         = CI_BASE_IMAGE
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

# Multi-arch image + wheel export. The group runs both targets in one bake
# invocation so BuildKit shares the layer cache (wheel export is instant).
group "test-rocm-ci-with-wheel" {
  targets = ["test-rocm-ci", "export-wheel-rocm"]
}

# Per-architecture CI targets — each builds for a single GPU arch and pushes
# to the registry so test agents can pull the image.
# Each per-arch build step sets IMAGE_TAG to e.g. rocm/vllm-ci:<commit>-gfx942
target "test-rocm-gfx90a-ci" {
  inherits   = ["test-rocm-gfx90a", "_ci-rocm"]
  cache-from = get_cache_from_rocm()
  cache-to   = get_cache_to_rocm()
  tags       = compact([IMAGE_TAG])
  output     = ["type=registry"]
}

target "test-rocm-gfx942-ci" {
  inherits   = ["test-rocm-gfx942", "_ci-rocm"]
  cache-from = get_cache_from_rocm()
  cache-to   = get_cache_to_rocm()
  tags       = compact([IMAGE_TAG])
  output     = ["type=registry"]
}

target "test-rocm-gfx950-ci" {
  inherits   = ["test-rocm-gfx950", "_ci-rocm"]
  cache-from = get_cache_from_rocm()
  cache-to   = get_cache_to_rocm()
  tags       = compact([IMAGE_TAG])
  output     = ["type=registry"]
}

# Image tags for the scheduled ci_base build (amd-ci-base.yaml pipeline).
# CI_BASE_IMAGE_TAG_DATED is set by the pipeline to e.g. rocm/vllm-dev:ci_base-20250330.
variable "CI_BASE_IMAGE_TAG" {
  default = "rocm/vllm-dev:ci_base"
}

variable "CI_BASE_IMAGE_TAG_DATED" {
  default = ""
}

# Scheduled target: builds only the ci_base stage (RIXL, DeepEP, torchcodec, etc.)
# Run weekly by the amd-ci-base pipeline; per-PR builds then pull the result as
# CI_BASE_IMAGE instead of rebuilding those slow layers on every commit.
target "ci-base-rocm-ci" {
  inherits   = ["_common-rocm", "_ci-rocm", "_labels"]
  target     = "ci_base"
  cache-from = get_cache_from_rocm()
  cache-to   = get_cache_to_rocm()
  tags = compact([
    CI_BASE_IMAGE_TAG,
    CI_BASE_IMAGE_TAG_DATED,
  ])
  output = ["type=registry"]
}
