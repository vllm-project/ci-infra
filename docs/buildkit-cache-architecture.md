# BuildKit Cache Architecture and Materialization

## Executive Summary

After analyzing the BuildKit source code, the most practical approach for pre-populating cache is **Option A: Run a build during AMI creation**. This approach naturally populates BuildKit's internal storage with extracted cache data that can be used immediately at runtime.

## BuildKit Internal Storage Format

### Storage Location

Default root: `/var/lib/buildkit`

### Directory Structure

```
/var/lib/buildkit/
├── cache.db           # BoltDB - solver cache keys and results
├── history.db         # BoltDB - build history
├── buildkitd.lock     # Lock file
├── workerid           # Worker identity file
├── containerd.sock    # (if using containerd worker)
├── runc-overlayfs/    # Snapshotter storage (depends on snapshotter type)
│   └── snapshots/     # Actual layer snapshots (extracted filesystem content)
└── content/           # Content-addressable blob storage
    └── ingest/        # Temporary ingestion area
    └── blobs/
        └── sha256/    # Blob files by digest
```

### Key Components

1. **Content Store** (`content/blobs/sha256/`):
   - Stores compressed layer blobs by their digest
   - OCI-compatible content-addressable storage
   - Blobs are the raw compressed tar archives

2. **Snapshotter** (`runc-overlayfs/` or similar):
   - Stores extracted filesystem snapshots
   - Each layer is unpacked into a snapshot
   - Snapshots are identified by chain ID (hash of layer digests)
   - Uses overlayfs for efficient layer stacking

3. **Cache Database** (`cache.db`):
   - BoltDB database
   - Buckets: `_result`, `_links`, `_byresult`, `_backlinks`
   - Maps build cache keys to results (layer snapshots)
   - Essential for cache hit detection

4. **Metadata Store**:
   - Tracks layer relationships, parent chains, and properties
   - Stored alongside content and snapshots

## Cache Manifest Format

The BuildKit cache manifest (`application/vnd.buildkit.cacheconfig.v0`) contains:

```json
{
  "layers": [
    {
      "blob": "sha256:...",      // Compressed blob digest
      "parentIndex": -1           // Index of parent layer (-1 for base)
    }
  ],
  "records": [
    {
      "digest": "...",           // Cache key digest
      "inputs": [...],           // Input cache keys
      "results": [...]           // Result references
    }
  ]
}
```

This manifest describes the DAG of cache entries and their relationships.

## Cache Import Flow

When BuildKit processes `--cache-from type=registry,ref=...`:

```
1. Resolve manifest from registry
   └── Fetch cache config manifest

2. Parse cache config
   └── Extract layer descriptors and cache records
   └── Build CacheChains structure (DAG of cache entries)

3. Create CacheKeyStorage and CacheResultStorage
   └── In-memory representation of cache

4. On cache hit during build:
   └── Call cacheResultStorage.Load()
   └── Call worker.FromRemote(remote)
       └── For each descriptor in remote:
           ├── Fetch blob from registry (via Provider)
           ├── Store in content store
           ├── Call CacheMgr.GetByBlob()
           │   ├── Check if snapshot exists by chain ID
           │   ├── If not, create lease
           │   ├── Extract/apply layer diff
           │   └── Create snapshot
           └── Return cache reference
```

**Key Insight**: The extraction (unpacking compressed blobs to snapshots) happens lazily when a cache hit is found and the result is needed. This is the expensive operation.

## Materialization Options Analysis

### Option A: Run a build during AMI creation ✅ RECOMMENDED

**Approach**:
```bash
# During Packer AMI build
git clone https://github.com/vllm-project/vllm.git
cd vllm
docker buildx build \
  --cache-from type=registry,ref=936637512419.dkr.ecr.us-east-1.amazonaws.com/vllm-ci-postmerge-cache:latest \
  --target test \
  -f docker/Dockerfile \
  --load \
  .
```

**What happens**:
1. BuildKit fetches cache manifest from registry
2. Starts building, hits cache for most steps
3. Downloads and extracts cached layers into `/var/lib/buildkit/`
4. Content store and snapshots are populated
5. Cache.db is updated with cache keys

**Pros**:
- Natural BuildKit flow, no hacks required
- Cache is in correct internal format
- All metadata is properly populated
- Works across instances (cache keys are content-based)
- Snapshots are pre-extracted

**Cons**:
- Requires running a build during AMI creation
- Need to match build args exactly for cache hits
- AMI build takes longer (~10-20 min)

**Portability**: Cache IS portable across instances because:
- Worker ID is just for identification, not for cache keys
- Cache keys are based on Dockerfile content and build args
- Snapshots are identified by chain ID (content-based)

### Option B: Use `buildctl` to import cache ❌ NOT AVAILABLE

There is no `buildctl cache import` command. Cache import only happens during builds via `--import-cache`.

### Option C: Directly populate BuildKit storage ❌ NOT PRACTICAL

Would require:
1. Fetching all blobs from registry
2. Extracting them using the correct diff applier
3. Creating proper snapshot metadata
4. Populating cache.db with correct keys

This essentially reimplements BuildKit's cache import logic. Not practical.

### Option D: Convert OCI layout to BuildKit format ❌ NOT SUPPORTED

The `--cache-from type=local` expects OCI layout but still needs to extract. There's no tool to convert OCI layout directly to BuildKit's internal snapshot format.

## Proof of Concept Implementation

### AMI Build Script Changes

Update `packer/cpu/scripts/pull-base-images.sh`:

```bash
#!/bin/bash
set -eu -o pipefail

echo "=== Pre-warming BuildKit cache ==="

# Clone vLLM repo
WORK_DIR="/tmp/vllm-cache-warmup"
mkdir -p "$WORK_DIR"
cd "$WORK_DIR"

git clone --depth 1 https://github.com/vllm-project/vllm.git .

# Authenticate to ECR
aws ecr get-login-password --region us-east-1 | \
  docker login --username AWS --password-stdin 936637512419.dkr.ecr.us-east-1.amazonaws.com

# Create buildx builder using the local buildkitd
docker buildx create --name cache-warmer --driver remote --use unix:///run/buildkit/buildkitd.sock || \
  docker buildx use cache-warmer

# Run build with cache-from to populate local cache
# This extracts all cached layers into /var/lib/buildkit
echo "Running build to populate cache..."
docker buildx build \
  --cache-from type=registry,ref=936637512419.dkr.ecr.us-east-1.amazonaws.com/vllm-ci-postmerge-cache:latest \
  --build-arg max_jobs=16 \
  --build-arg USE_SCCACHE=1 \
  --build-arg TORCH_CUDA_ARCH_LIST="8.0 8.9 9.0 10.0" \
  --build-arg FI_TORCH_CUDA_ARCH_LIST="8.0 8.9 9.0a 10.0a" \
  --target test \
  -f docker/Dockerfile \
  --load \
  . 2>&1 | tee /tmp/cache-warmup.log

# Show cache size
echo "=== Cache populated ==="
du -sh /var/lib/buildkit
ls -la /var/lib/buildkit/

# Cleanup
cd /
rm -rf "$WORK_DIR"
docker buildx rm cache-warmer || true

echo "=== Cache warmup complete ==="
```

### CI Template Changes

Keep the CI template simple - just connect to buildkitd and use local cache:

```yaml
- |
  # Connect to pre-baked buildkitd with warm cache
  if [[ -S /run/buildkit/buildkitd.sock ]]; then
    docker buildx create --name baked-vllm-builder --driver remote --use unix:///run/buildkit/buildkitd.sock
  else
    # Fallback to fresh builder
    docker buildx create --name baked-vllm-builder --driver docker-container --use --bootstrap
  fi
```

The build should now hit the pre-extracted cache immediately.

## Data Flow Diagram

```
AMI Build Time:
┌──────────────────────────────────────────────────────────────────────┐
│  Packer AMI Build                                                     │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────────────────┐  │
│  │  ECR Cache  │───►│  BuildKit   │───►│  /var/lib/buildkit/     │  │
│  │  Registry   │    │  (extract)  │    │  ├── content/blobs/     │  │
│  └─────────────┘    └─────────────┘    │  ├── runc-overlayfs/    │  │
│                                         │  └── cache.db           │  │
│                                         └─────────────────────────┘  │
│                                                    │                  │
│                                                    ▼                  │
│                                         ┌─────────────────────┐      │
│                                         │    EBS Snapshot     │      │
│                                         │    (AMI)            │      │
│                                         └─────────────────────┘      │
└──────────────────────────────────────────────────────────────────────┘

CI Runtime:
┌──────────────────────────────────────────────────────────────────────┐
│  CI Build                                                             │
│  ┌─────────────────────┐    ┌─────────────┐    ┌─────────────────┐  │
│  │  /var/lib/buildkit/ │───►│  BuildKit   │───►│  Build Output   │  │
│  │  (pre-populated)    │    │  (cache hit)│    │  (fast!)        │  │
│  └─────────────────────┘    └─────────────┘    └─────────────────┘  │
│                                    │                                  │
│                              No extraction!                           │
│                              Cache already                            │
│                              in internal format                       │
└──────────────────────────────────────────────────────────────────────┘
```

## Success Criteria Validation

With Option A implementation:

1. ✅ **Show "CACHED" immediately**: Snapshots are pre-extracted
2. ✅ **No "importing cache manifest" time**: Cache is already local
3. ✅ **No "extracting" steps**: Layers are already unpacked in snapshots
4. ✅ **Portable across instances**: Cache keys are content-based

## Recommendations

1. **Implement Option A** - Run a build during AMI creation
2. **Match build args exactly** - Cache keys depend on build args
3. **Keep EBS performance high** - Fast disk still matters for reading snapshots
4. **Monitor cache hit rates** - Log whether builds are hitting cache

## References

- BuildKit Source: https://github.com/moby/buildkit
- Cache Manager: `cache/manager.go`
- Remote Cache Import: `cache/remotecache/import.go`
- Worker FromRemote: `worker/base/worker.go:547`
- BoltDB Cache Storage: `solver/bboltcachestorage/storage.go`
