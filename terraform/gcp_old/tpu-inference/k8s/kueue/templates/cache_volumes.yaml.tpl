---
# The caches, as claims a pod can name without knowing where they live.
#
# Static PersistentVolumes rather than inline CSI volumes in the workload. The
# bucket is regional (see cache.tf), so its name differs per cluster, and in
# the workload it would mean every manifest and step carrying a bucket name it
# has no business knowing. Here the claim names are identical everywhere and
# resolve at mount time, in whichever cluster MultiKueue actually landed the
# pod in - which also has to be here rather than created by the workload,
# because MultiKueue copies only the Job. gcsfuse serves ReadWriteMany, so
# every pod mounts the same claim at once.
apiVersion: v1
kind: PersistentVolume
metadata:
  name: jax-cache
spec:
  accessModes:
    - ReadWriteMany
  # Ignored by the driver - a bucket has no size - but the API requires it and
  # the claim below has to ask for the same number.
  capacity:
    storage: 1Gi
  storageClassName: ""
  persistentVolumeReclaimPolicy: Retain
  claimRef:
    namespace: ${NAMESPACE}
    name: jax-cache
  mountOptions:
    - implicit-dirs
    # Scoped to this cache, not the whole bucket, so the metadata prefetch does
    # not walk every other kind of cache in it at mount time. A second kind gets
    # its own PersistentVolume with its own only-dir.
    - only-dir=jax_cache
    # Thousands of small content-addressed files, read far more often than
    # written. Never expire metadata: every access stats the object, and at the
    # default 60s TTL a compile-heavy step spends its time re-stating over the
    # network. Safe to pin because an entry's name encodes its contents, so a
    # name never changes meaning.
    - metadata-cache:ttl-secs:-1
    - metadata-cache:stat-cache-max-size-mb:-1
    - metadata-cache:type-cache-max-size-mb:-1
    - file-system:kernel-list-cache-ttl-secs:-1
    # A compilation cache is mostly misses, and at the 5s default every "does
    # this exist" that comes back no is another round trip. Bounded rather than
    # infinite because this mount is written during a run.
    - metadata-cache:negative-ttl-secs:60
    # 1MiB, not the 128MiB the model mount uses: reading ahead of a 4KiB cache
    # entry buys nothing and costs bandwidth on every lookup. Parallel downloads
    # are off for the same reason - they exist for reads over a gigabyte.
    - read_ahead_kb=1024
    - file-cache:enable-parallel-downloads:false
    - write:enable-streaming-writes:true
  csi:
    driver: gcsfuse.csi.storage.gke.io
    volumeHandle: ${CACHE_BUCKET}
    volumeAttributes:
      # Load this prefix's metadata in one batch at mount instead of a round
      # trip per lookup. Requires the unbounded caches above.
      gcsfuseMetadataPrefetchOnMount: "true"
      # Redundant here: every pod uses the same service account against the same
      # bucket, and the check costs IAM and STS calls on the startup path.
      skipCSIBucketAccessCheck: "true"
      # 8Gi against the model mount's 65Gi: a run reads a fraction of a
      # namespace that is only ~34GB whole, so the rest of the node's cache is
      # worth more to the models. Explicit rather than -1, which fills the
      # volume and then starts failing writes.
      #
      # One figure, not one per machine type: a PersistentVolume is one object
      # per cluster and every profile binds the same claim, so it has to hold on
      # the smallest shape we run - ct6e-standard-1t's 176 GB.
      fileCacheCapacity: "8Gi"
      # Defaults to false, which sends random reads past the cache to GCS.
      fileCacheForRangeRead: "true"
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: jax-cache
  namespace: ${NAMESPACE}
spec:
  accessModes:
    - ReadWriteMany
  resources:
    requests:
      storage: 1Gi
  storageClassName: ""
  volumeName: jax-cache
---
# Model weights: few large files read sequentially, the opposite shape to the
# compilation cache, so tuned the opposite way. Its own bucket rather than a
# second prefix of the one above, because the driver keys a volume by bucket
# name and two PersistentVolumes over one bucket become a single mount.
apiVersion: v1
kind: PersistentVolume
metadata:
  name: hf-cache
spec:
  accessModes:
    - ReadWriteMany
  # Ignored, as above - worth repeating because this mount holds multi-gigabyte
  # checkpoints and 1Gi reads like a limit. It is not one. What bounds anything
  # is fileCacheCapacity below, which sizes the on-node cache.
  capacity:
    storage: 1Gi
  storageClassName: ""
  persistentVolumeReclaimPolicy: Retain
  claimRef:
    namespace: ${NAMESPACE}
    name: hf-cache
  mountOptions:
    - implicit-dirs
    # A minute, where the compilation cache pins its metadata forever. This
    # mount is not read-only: a model nothing has fetched yet is downloaded by
    # the first workload that wants it, through this mount, and the same
    # process then lists the directory it just filled. Pinned metadata makes
    # that listing the one taken before the download - vLLM reports "Cannot
    # find any model weights" for a snapshot whose safetensors are sitting in
    # the bucket - and a handle opened across the change comes back as
    # OSError: [Errno 116] Stale file handle.
    #
    # The size caps stay unbounded. What has to expire is an entry's age, not
    # how many of them are kept, and a checkpoint directory is a few dozen
    # names.
    - metadata-cache:ttl-secs:60
    - metadata-cache:stat-cache-max-size-mb:-1
    - metadata-cache:type-cache-max-size-mb:-1
    - file-system:kernel-list-cache-ttl-secs:60
    # Hugging Face probes several optional files per model -
    # adapter_config.json and friends - that legitimately do not exist, so a
    # negative entry is worth keeping. The same minute as above, and for the
    # same reason: what does not exist at the start of a download does by the
    # end of it.
    - metadata-cache:negative-ttl-secs:60
    # 128MiB, GKE's own serving-profile value. Large sequential reads want each
    # round trip to carry as much as possible.
    - read_ahead_kb=131072
    - file-cache:enable-parallel-downloads:true
    - file-cache:parallel-downloads-per-file:8
    - file-cache:download-chunk-size-mb:64
    - write:enable-streaming-writes:true
  csi:
    driver: gcsfuse.csi.storage.gke.io
    volumeHandle: ${MODELS_BUCKET}
    volumeAttributes:
      gcsfuseMetadataPrefetchOnMount: "true"
      skipCSIBucketAccessCheck: "true"
      # 65Gi, which holds the whole working set. fileCacheForRangeRead below
      # pulls an object into the cache on a partial read, and gcsfuse will not
      # cache an object that does not fit the remaining capacity, so a capacity
      # under the largest checkpoint refetches it on every load and evicts the
      # small models that would have fit.
      #
      # The pod backs this with a gke-gcsfuse-cache volume, and shares that one
      # volume with every other gcsfuse mount - so it has to hold this plus the
      # jax cache's 8Gi, not just this. Too small is not a slower mount: the
      # sidecar falls back to 5GiB of ephemeral storage, or the kubelet evicts
      # the pod when the tmpfs fills. The launcher sizes the volume at half the
      # machine type's memory, and generate_manifests.py refuses a shape whose
      # half does not clear the total.
      fileCacheCapacity: "65Gi"
      # safetensors are memory-mapped, which is nothing but random reads. With
      # this false a 2.88 GiB checkpoint page-faulted over the network until the
      # server timed out.
      fileCacheForRangeRead: "true"
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: hf-cache
  namespace: ${NAMESPACE}
spec:
  accessModes:
    - ReadWriteMany
  resources:
    requests:
      storage: 1Gi
  storageClassName: ""
  volumeName: hf-cache
