# BuildKit Cache Materialization Research Plan

## Objective

Understand how BuildKit stores and retrieves cache internally, and determine how to pre-populate `/var/lib/buildkit` with cache data that BuildKit can use directly at runtime—without extraction overhead.

## Background Context

We are building AMIs for CI with pre-warmed Docker build cache. Current challenges:

1. **Registry cache (`--cache-from type=registry`)**: Works but requires extraction at build time (CPU + disk I/O bound)

2. **OCI layout cache (`--cache-from type=local`)**: We tried using `regctl` to copy cache manifests from ECR to local OCI layout, but BuildKit still needs to extract/decompress at runtime

3. **Goal**: Have cache already in BuildKit's internal format in the AMI, so builds can use it immediately with zero extraction overhead

## Research Questions

### 1. BuildKit Internal Storage Format

- How does BuildKit store cached layers internally in `/var/lib/buildkit`?
- What is the directory structure? (e.g., `snapshots/`, `content/`, `metadata/`, etc.)
- What database/metadata files does BuildKit use? (boltdb? sqlite?)
- How do content-addressable blobs map to layer snapshots?

### 2. Cache Manifest Format

- What is the `application/vnd.buildkit.cacheconfig.v0` manifest format?
- How does this manifest reference content blobs?
- What metadata is stored alongside the manifest?

### 3. Cache Import Flow

When BuildKit processes `--cache-from type=registry,ref=...`:
- What steps does it perform?
- When does decompression happen?
- When are snapshots created?
- What gets written to `/var/lib/buildkit`?

### 4. Materialization Options

Investigate these potential approaches:

**Option A: Run a build during AMI creation**
- Clone repo, run `docker buildx build --cache-from type=registry ...`
- Let BuildKit naturally populate `/var/lib/buildkit`
- Snapshot AMI with populated cache
- Question: Will this cache be usable on a different instance?

**Option B: Use `buildctl` to import cache**
- Is there a `buildctl` command to import cache without building?
- Something like `buildctl cache import --from registry,...`?

**Option C: Directly populate BuildKit storage**
- Can we reconstruct `/var/lib/buildkit` contents from registry blobs?
- What tools exist for this? (crane, regctl, custom scripts?)
- What are the required metadata files?

**Option D: Use BuildKit's local exporter/importer**
- `--cache-to type=local` exports to OCI layout
- Can we convert OCI layout to BuildKit's internal format?
- Is there a tool or API for this?

## Key Source Files to Examine

In the moby/buildkit repository (https://github.com/moby/buildkit):

### Cache Storage
- `cache/` - Cache management
- `cache/metadata/` - Metadata storage
- `cache/remotecache/` - Remote cache import/export

### Content Store
- `content/` - Content-addressable storage
- `content/local/` - Local content store implementation

### Snapshotter
- `snapshot/` - Snapshot management
- `snapshot/containerd/` - Containerd snapshotter integration

### Remote Cache
- `cache/remotecache/registry/` - Registry cache backend
- `cache/remotecache/local/` - Local cache backend
- `solver/llbsolver/` - How cache is resolved during builds

### Key Files
- `client/llb/state.go` - LLB state definitions
- `solver/cachekey.go` - Cache key computation
- `cache/manager.go` - Cache manager implementation

## Expected Deliverables

1. **Architecture Document**: Explain how BuildKit cache storage works internally

2. **Data Flow Diagram**: Show the path from registry cache → internal storage

3. **Materialization Procedure**: Step-by-step process to pre-populate `/var/lib/buildkit`

4. **Proof of Concept**: Commands or script to test the approach

5. **Trade-offs Analysis**: Pros/cons of each materialization option

## Constraints

- Cache must be usable across different instances (not tied to specific machine IDs)
- Must work with BuildKit version `buildx-stable-1` (moby/buildkit image)
- Should integrate with our Packer AMI build process
- Target cache size is ~10-25GB

## Success Criteria

A build using the pre-populated cache should:
1. Show "CACHED" for previously-built layers immediately
2. Not spend time on "importing cache manifest" or "extracting" steps
3. Behave identically to a warm cache from a previous build on the same machine

## References

- BuildKit repo: https://github.com/moby/buildkit
- BuildKit cache documentation: https://github.com/moby/buildkit/blob/master/docs/buildkitd.toml.md
- Docker buildx cache docs: https://docs.docker.com/build/cache/backends/
- OCI Image Spec: https://github.com/opencontainers/image-spec
- Containerd content store: https://github.com/containerd/containerd/blob/main/docs/content-flow.md
