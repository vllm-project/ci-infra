# ci-rocm.hcl - CI-specific configuration for vLLM ROCm Docker builds
#
# This file lives in ci-infra repo at docker/ci-rocm.hcl
# Used with: docker buildx bake -f docker/docker-bake-rocm.hcl -f ci-rocm.hcl test-rocm-ci
#
# Registry cache: Docker Hub (rocm/vllm-ci-cache) is used exclusively.
# AMD build agents already have Docker Hub credentials (they push the test
# image to rocm/vllm-ci), so no additional credential setup is required.
# ROCm CI does not use a separate remote compiler cache.

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

# Merge-base of HEAD with main - provides a more stable cache fallback than
# parent commit for long-lived PRs. Mirrors the VLLM_MERGE_BASE_COMMIT
# pattern used in the shared ci.hcl file. Auto-computed by ci-bake-rocm.sh
# when unset.
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

# Leave CI_MAX_JOBS empty so the Dockerfile falls back to $(nproc) and uses
# the full builder parallelism. Operators can still override this per build.
variable "CI_MAX_JOBS" {
  default = ""
}

# Docker Hub registry cache for AMD builds.
#
# A separate repo (rocm/vllm-ci-cache) is used for BuildKit layer cache.
# cache-to uses mode=min to reduce the volume of data pushed.
# NOTE: mode=min still includes all layers referenced by the final image
# manifest, including inherited base layers (~7.25GB ROCm runtime).
# Docker Hub auto-creates the repo on first push.
#
# Final-image cache stays commit-scoped. Branch-to-branch reuse for the test
# image comes from importing the parent and merge-base commit cache refs.
#
# The source-scoped native cache is exported both per-commit and per-branch so
# ROCm extension rebuilds are shareable within the same commit reruns and across
# consecutive commits on the same branch without depending on a single global
# latest tag.

variable "DOCKERHUB_CACHE_REPO" {
  default = "rocm/vllm-ci-cache"
}

variable "DOCKERHUB_CACHE_TO" {
  default = ""
}

variable "ROCM_CACHE_BRANCH_TAG" {
  default = ""
}

variable "ROCM_CACHE_UPSTREAM_BRANCH_TAG" {
  default = ""
}

# Functions

function "get_cache_from_rocm" {
  params = []
  result = compact([
    # Exact commit hit - fastest cache on re-runs of the same commit
    BUILDKITE_COMMIT != "" ? "type=registry,ref=${DOCKERHUB_CACHE_REPO}:rocm-${BUILDKITE_COMMIT}" : "",
    # Parent commit - useful cache for incremental changes
    PARENT_COMMIT != "" ? "type=registry,ref=${DOCKERHUB_CACHE_REPO}:rocm-${PARENT_COMMIT}" : "",
    # Merge-base with main - stable fallback for long-lived or rebased PRs;
    # maps to a real main-branch commit whose cache layers are likely warm
    VLLM_MERGE_BASE_COMMIT != "" ? "type=registry,ref=${DOCKERHUB_CACHE_REPO}:rocm-${VLLM_MERGE_BASE_COMMIT}" : "",
    # Import the source-scoped native build cache as well so builds whose
    # Python/package layers changed can still reuse compiled ROCm objects.
    BUILDKITE_COMMIT != "" ? "type=registry,ref=${DOCKERHUB_CACHE_REPO}:csrc-rocm-${BUILDKITE_COMMIT}" : "",
    PARENT_COMMIT != "" ? "type=registry,ref=${DOCKERHUB_CACHE_REPO}:csrc-rocm-${PARENT_COMMIT}" : "",
    VLLM_MERGE_BASE_COMMIT != "" ? "type=registry,ref=${DOCKERHUB_CACHE_REPO}:csrc-rocm-${VLLM_MERGE_BASE_COMMIT}" : "",
    ROCM_CACHE_BRANCH_TAG != "" ? "type=registry,ref=${DOCKERHUB_CACHE_REPO}:csrc-rocm-branch-${ROCM_CACHE_BRANCH_TAG}" : "",
    ROCM_CACHE_UPSTREAM_BRANCH_TAG != "" ? "type=registry,ref=${DOCKERHUB_CACHE_REPO}:csrc-rocm-branch-${ROCM_CACHE_UPSTREAM_BRANCH_TAG}" : "",
  ])
}

function "get_cache_to_rocm" {
  params = []
  result = compact([
    # Keep the final-image cache commit-scoped. Exporting both rocm-$commit and
    # rocm-latest from the same bake caused duplicate registry cache exporters
    # and flaky 400 responses from Docker Hub.
    BUILDKITE_COMMIT != "" ? "type=registry,ref=${DOCKERHUB_CACHE_REPO}:rocm-${BUILDKITE_COMMIT},mode=min" : "",
  ])
}

function "get_cache_from_rocm_csrc" {
  params = []
  result = compact([
    BUILDKITE_COMMIT != "" ? "type=registry,ref=${DOCKERHUB_CACHE_REPO}:csrc-rocm-${BUILDKITE_COMMIT}" : "",
    PARENT_COMMIT != "" ? "type=registry,ref=${DOCKERHUB_CACHE_REPO}:csrc-rocm-${PARENT_COMMIT}" : "",
    VLLM_MERGE_BASE_COMMIT != "" ? "type=registry,ref=${DOCKERHUB_CACHE_REPO}:csrc-rocm-${VLLM_MERGE_BASE_COMMIT}" : "",
    ROCM_CACHE_BRANCH_TAG != "" ? "type=registry,ref=${DOCKERHUB_CACHE_REPO}:csrc-rocm-branch-${ROCM_CACHE_BRANCH_TAG}" : "",
    ROCM_CACHE_UPSTREAM_BRANCH_TAG != "" ? "type=registry,ref=${DOCKERHUB_CACHE_REPO}:csrc-rocm-branch-${ROCM_CACHE_UPSTREAM_BRANCH_TAG}" : "",
  ])
}

function "get_cache_to_rocm_csrc" {
  params = []
  result = compact([
    # Export the exact-commit native cache for same-commit reruns.
    BUILDKITE_COMMIT != "" ? "type=registry,ref=${DOCKERHUB_CACHE_REPO}:csrc-rocm-${BUILDKITE_COMMIT},mode=min" : "",
    # Export the branch-scoped native cache so later commits on the same branch
    # can reuse compiled ROCm objects even when the exact parent cache is absent.
    ROCM_CACHE_BRANCH_TAG != "" ? "type=registry,ref=${DOCKERHUB_CACHE_REPO}:csrc-rocm-branch-${ROCM_CACHE_BRANCH_TAG},mode=min" : "",
  ])
}

# CI targets

target "_ci-rocm" {
  annotations = [
    "manifest:vllm.buildkite.build_number=${BUILDKITE_BUILD_NUMBER}",
    "manifest:vllm.buildkite.build_id=${BUILDKITE_BUILD_ID}",
  ]
  args = {
    ARG_PYTORCH_ROCM_ARCH = PYTORCH_ROCM_ARCH
    CI_BASE_IMAGE         = CI_BASE_IMAGE
    max_jobs              = CI_MAX_JOBS
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

# Cache-only target for the source-scoped ROCm native build stage.
# This persists the csrc-build stage in the registry cache even though the
# final test image only consumes it indirectly while packaging the wheel.
target "csrc-rocm-ci" {
  inherits   = ["_common-rocm", "_ci-rocm"]
  target     = "csrc-build"
  cache-from = get_cache_from_rocm_csrc()
  cache-to   = get_cache_to_rocm_csrc()
  output     = ["type=cacheonly"]
}

# Keep wheel export on the same CI graph as the test image build so the
# shared build_vllm/export_vllm stages resolve identically within one bake
# invocation. Without this, export-wheel-rocm uses the plain local target
# args while test-rocm-ci uses CI-only args, which can lead to separate
# cache lineages and inconsistent export_vllm results.
target "export-wheel-rocm" {
  inherits   = ["_common-rocm", "_ci-rocm"]
  target     = "export_vllm"
  cache-from = get_cache_from_rocm()
  output     = ["type=local,dest=./wheel-export"]
}

# Multi-arch image + wheel export. The group runs both targets in one bake
# invocation so BuildKit shares the layer cache. csrc-rocm-ci makes the
# source-scoped native build cache persist across commits even when the final
# image layers change for unrelated reasons.
group "test-rocm-ci-with-wheel" {
  targets = ["csrc-rocm-ci", "test-rocm-ci", "export-wheel-rocm"]
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
# Use inline cache metadata on the ci_base image itself instead of exporting a
# separate registry cache artifact. That keeps rebuilds warm without duplicating
# the full ROCm base image into rocm/vllm-ci-cache.
target "ci-base-rocm-ci" {
  inherits   = ["_common-rocm", "_ci-rocm", "_labels"]
  target     = "ci_base"
  cache-from = compact([
    "type=registry,ref=${CI_BASE_IMAGE_TAG}",
    CI_BASE_IMAGE_TAG_DATED != "" ? "type=registry,ref=${CI_BASE_IMAGE_TAG_DATED}" : "",
  ])
  cache-to = ["type=inline"]
  tags = compact([
    CI_BASE_IMAGE_TAG,
    CI_BASE_IMAGE_TAG_DATED,
  ])
  output = ["type=registry"]
}
