# The caches, as claims a pod can name without knowing where they live.
#
# One bucket each, mounted at its root.
#
# Not two prefixes of one bucket: the CSI driver identifies a volume by
# volumeHandle, which is the bucket name, so two PersistentVolumes over one
# bucket are a single volume to the kubelet - it mounts once and both
# mountPaths land on the same directory. That is not a hypothetical; it is what
# happened, and /cache/jax listed the model cache.
#
# Separate buckets also let the two keep their own tuning. The compilation
# cache still mounts a prefix rather than the bucket root, because that bucket
# holds more than one kind of cache; the models bucket is HF's own layout and
# mounts at its root.
#
# Static PersistentVolumes rather than inline CSI volumes in the workload
# manifest. The bucket is regional - a cache 10,000 km away costs 502ms per
# miss against 36ms in-region - so the name differs per cluster, and putting it
# in the workload would mean every manifest, pipeline and step carrying a
# bucket name it has no business knowing.
#
# Here the claim names are identical in every region and the binding is
# infrastructure. It also survives MultiKueue placing a job somewhere other than
# where it was submitted: the claim resolves at mount time, in whichever cluster
# the pod actually landed in.
#
# Created here rather than by the workload because MultiKueue copies only the
# Job. A claim created where the launcher runs would not exist where the pod
# does; these are static and shared, and gcsfuse serves ReadWriteMany, so every
# pod mounts the same claim at once.
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
    # Scoped to this cache, not the whole bucket. The bucket is per-purpose but
    # will hold more than one kind of cache, so the compilation cache lives
    # under its own prefix and the mount says so - otherwise the metadata
    # prefetch walks every other type at mount time to reach this one.
    #
    # A second cache type gets its own PersistentVolume with its own only-dir.
    # Two PVs over one bucket needs a unique volumeHandle in the form
    # BUCKET:SUFFIX, supported from GKE 1.33.0-gke.1932000; this cluster runs
    # 1.35.6. Without it they are one volume to the kubelet and both mounts
    # land on the same directory - which is how /cache/jax came to list the
    # model cache.
    - only-dir=jax_cache
    # Thousands of small content-addressed files, read far more often than
    # written. Never expire metadata: every access stats the object, the
    # default TTL is 60s, and re-stating over the network is what a
    # compile-heavy step would otherwise spend its time on. Safe to pin because
    # an entry's name encodes its contents, so a name never changes meaning.
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
      # Redundant here: every pod uses the same node service account against
      # the same bucket, and the check costs IAM and STS calls on the startup
      # path.
      skipCSIBucketAccessCheck: "true"
      # 8Gi. Compilation entries are thousands of small files and a run reads
      # a fraction of the namespace; the capacity this was holding is worth
      # more to the model mount, which was starved.
      # Explicit rather than -1: the capacity is the threshold gcsfuse evicts
      # against, and with -1 it fills the volume and starts failing writes.
      # That cost build 241 26.6m of checkpoint loading, against 8.8m for
      # build 234's explicit capacities in a smaller volume.
      #
      # A fixed figure, not one per machine type, because a PersistentVolume
      # is one object per cluster and every profile's pods bind the same
      # claim - so it has to hold on the smallest shape we run. Sized for
      # ct6e-standard-1t's 176 GB at 0.40 for models and 0.05 for the
      # compilation cache, the latter capped at 30Gi on larger machines
      # because the whole namespace is ~34GB. Both are conservative above
      # ct6e-standard-1t. The pod's cache volume is sized per machine type
      # by the launcher and stays above the 73Gi these two sum to.
      #
      # 65Gi rather than the 73Gi first tried: build 242 loaded checkpoints
      # in 9.5m at 73Gi against 8.8m at 56Gi, so the working set already fits
      # and further capacity buys nothing. On a 176 GB node the difference is
      # ~25 GiB returned to the tests, which is the scarce resource here.
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
# compilation cache, so tuned the opposite way.
apiVersion: v1
kind: PersistentVolume
metadata:
  name: hf-cache
spec:
  accessModes:
    - ReadWriteMany
  # Ignored, as above, and worth repeating here because this mount holds
  # multi-gigabyte checkpoints and 1Gi reads like a limit. It is not one: the
  # driver never enforces capacity on a bucket. What does bound anything is
  # fileCacheCapacity below, which sizes the on-node cache.
  capacity:
    storage: 1Gi
  storageClassName: ""
  persistentVolumeReclaimPolicy: Retain
  claimRef:
    namespace: ${NAMESPACE}
    name: hf-cache
  mountOptions:
    - implicit-dirs
    - metadata-cache:ttl-secs:-1
    - metadata-cache:stat-cache-max-size-mb:-1
    - metadata-cache:type-cache-max-size-mb:-1
    - file-system:kernel-list-cache-ttl-secs:-1
    # An hour, against the compilation cache's minute. Hugging Face probes
    # several optional files per model - adapter_config.json and friends - that
    # legitimately do not exist, and a model that is absent stays absent until
    # something downloads it, which writes through this same mount.
    - metadata-cache:negative-ttl-secs:3600
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
      # 56Gi, not 20Gi. fileCacheForRangeRead below pulls a whole object into
      # the cache on a partial read, and gcsfuse will not cache an object that
      # does not fit the remaining capacity - so part2's gemma-4 checkpoints,
      # read a few layers at a time, were refetched on every one of 33 loads
      # and evicted the small models that would have fit. 84.6m of build 230's
      # 111.8m went on checkpoint loading; at 56Gi build 232 did the same work
      # in 23.8m and the step reached parity with bare metal.
      #
      # The pod must back this with a gke-gcsfuse-cache volume at least this
      # large, or the sidecar falls back to 5GiB of ephemeral storage and the
      # capacity is nominal. tpu-inference's test.yaml sizes that volume from
      # the launcher's FUSE_CACHE_SIZE for that reason, never below 81Gi.
      # Explicit rather than -1: the capacity is the threshold gcsfuse evicts
      # against, and with -1 it fills the volume and starts failing writes.
      # That cost build 241 26.6m of checkpoint loading, against 8.8m for
      # build 234's explicit capacities in a smaller volume.
      #
      # A fixed figure, not one per machine type, because a PersistentVolume
      # is one object per cluster and every profile's pods bind the same
      # claim - so it has to hold on the smallest shape we run. Sized for
      # ct6e-standard-1t's 176 GB at 0.40 for models and 0.05 for the
      # compilation cache, the latter capped at 30Gi on larger machines
      # because the whole namespace is ~34GB. Both are conservative above
      # ct6e-standard-1t. The pod's cache volume is sized per machine type
      # by the launcher and stays above the 73Gi these two sum to.
      #
      # 65Gi rather than the 73Gi first tried: build 242 loaded checkpoints
      # in 9.5m at 73Gi against 8.8m at 56Gi, so the working set already fits
      # and further capacity buys nothing. On a 176 GB node the difference is
      # ~25 GiB returned to the tests, which is the scarce resource here.
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
