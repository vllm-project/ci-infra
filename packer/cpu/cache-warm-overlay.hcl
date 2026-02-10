# cache-warm-overlay.hcl - Overlay for AMI cache warming
#
# This inherits from test-ci (from the CI build artifact) and overrides:
# - output: cacheonly (don't push image)
# - cache-to: empty (don't push cache)
# - tags: empty (no image to tag)
# - cache-from: overridden via --set at runtime for the commit-specific cache
#
# Usage:
#   docker buildx bake -f bake-config.json -f cache-warm-overlay.hcl \
#     --set "cache-warm.cache-from=[type=registry,ref=.../vllm-ci-test-cache:COMMIT,mode=max]" \
#     cache-warm

target "cache-warm" {
  inherits   = ["test-ci"]
  output     = ["type=cacheonly"]
  cache-to   = []
  tags       = []
}
