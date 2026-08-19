# A cache bucket per worker cluster, in that cluster's own region.
#
# The caches are the largest single lever on how efficiently the fleet uses its
# chips, and distance is what decides their cost. Measured from a pod against a
# bucket 10,000 km away, a compilation-cache miss took 502ms; against a bucket
# in the cluster's region, 36ms. A compile-heavy step asks whether an entry
# exists far more often than it reads one, so that difference moved a full suite
# from 1.60x bare metal's chip-minutes to 0.80x.
#
# Generated from var.worker_clusters, the same map that creates the clusters,
# their node pools and their queues - so a new region gets a co-located bucket
# without anyone remembering to make one.

locals {
  # Zone to region: southamerica-west1-a -> southamerica-west1. Same expression
  # clusters.tf already uses for the NAT routers.
  cache_region = {
    for k, v in var.worker_clusters :
    k => join("-", slice(split("-", v.location), 0, 2))
  }

  # Bucket names are globally unique across all of GCP and cannot be renamed -
  # changing one destroys and recreates it - so the name carries a hash of what
  # makes it ours. The inputs are things that never change for a given bucket:
  # project, cluster key, purpose. Nothing that scales or gets retuned goes in,
  # or resizing a pool would silently propose replacing the cache.
  #
  # Hashed rather than spelled out because names cap at 63 characters and
  # "cloud-tpu-inference-test" plus "southamerica-west1" leaves little room.
  # The readable facts are labels instead; nothing but terraform and the
  # PersistentVolume ever refers to the name.
  # One bucket per cache, not one bucket with two prefixes.
  #
  # The CSI driver identifies a volume by volumeHandle, which is the bucket
  # name, so two PersistentVolumes on one bucket are one volume to the kubelet:
  # it mounts once and both mountPaths land on the same directory. That is
  # exactly what happened - /cache/jax listed the model cache.
  #
  # Separating them also lets the two have their own retention, which they
  # want: compilation output is cheap to recreate and churns constantly, while
  # a model is expensive to fetch and rarely changes.
  cache_bucket = {
    for k, v in var.worker_clusters :
    k => format("%s-cache-%s-%s", var.name_prefix, local.cache_region[k],
    substr(sha256("${v.project}/${k}/cache"), 0, 6))
  }

  models_bucket = {
    for k, v in var.worker_clusters :
    k => format("%s-models-%s-%s", var.name_prefix, local.cache_region[k],
    substr(sha256("${v.project}/${k}/models"), 0, 6))
  }
}

resource "google_storage_bucket" "cache" {
  for_each = var.worker_clusters

  name     = local.cache_bucket[each.key]
  project  = each.value.project
  location = local.cache_region[each.key]

  uniform_bucket_level_access = true
  storage_class               = "STANDARD"

  # Real folders, chosen now because it cannot be chosen later: hierarchical
  # namespace is fixed when a bucket is created and changing it means replacing
  # the bucket.
  #
  # It matters here because of how gcsfuse writes. Every write goes to a
  # temporary object and is then renamed, and on a flat bucket a rename is a
  # copy followed by a delete. HNS makes it a folder operation - atomic, and
  # with up to 8x the initial QPS limit. Requires uniform bucket-level access,
  # which is set above.
  #
  # What it costs: no object versioning, retention lock, bucket lock,
  # cross-bucket replication or object-level ACLs. A rebuildable cache uses
  # none of those.
  hierarchical_namespace {
    enabled = true
  }

  # No soft delete. It is on by default with a seven-day retention, and
  # soft-deleted objects keep accruing storage charges for the whole of it.
  #
  # That is the wrong default for this bucket specifically: gcsfuse renames
  # through copy-and-delete, and the lifecycle rule below deletes on a schedule,
  # so a cache generates deletions continuously. Retaining a week of them would
  # cost more than the cache itself on a bucket that turns over every 30 days -
  # to protect data that is, by construction, recomputable.
  soft_delete_policy {
    retention_duration_seconds = 0
  }

  # Nothing here is public and nothing should become public by accident. The
  # bucket holds model weights and compilation output for a CI fleet.
  public_access_prevention = "enforced"

  # The cache is rebuildable by definition - a lost entry costs a recompile,
  # not data - but rebuilding all of it costs about 2.7x a suite's chips, so
  # do not let a terraform mistake take it.
  force_destroy = false

  lifecycle_rule {
    condition {
      age = var.cache_lifecycle_age_days
    }
    action {
      type = "Delete"
    }
  }

  # Resumable uploads that never finished. gcsfuse uploads large objects in
  # parts, and a pod evicted or killed mid-write leaves those parts behind,
  # billed as storage and invisible to an object listing.
  lifecycle_rule {
    condition {
      age = 1
    }
    action {
      type = "AbortIncompleteMultipartUpload"
    }
  }

  labels = merge(var.labels, {
    purpose = "tpu-ci-cache"
    cluster = each.key
    region  = local.cache_region[each.key]
  })
}

resource "google_storage_bucket" "models" {
  for_each = var.worker_clusters

  name     = local.models_bucket[each.key]
  project  = each.value.project
  location = local.cache_region[each.key]

  uniform_bucket_level_access = true
  storage_class               = "STANDARD"

  # Real folders, chosen now because it cannot be chosen later: hierarchical
  # namespace is fixed when a bucket is created and changing it means replacing
  # the bucket.
  #
  # It matters here because of how gcsfuse writes. Every write goes to a
  # temporary object and is then renamed, and on a flat bucket a rename is a
  # copy followed by a delete. HNS makes it a folder operation - atomic, and
  # with up to 8x the initial QPS limit. Requires uniform bucket-level access,
  # which is set above.
  #
  # What it costs: no object versioning, retention lock, bucket lock,
  # cross-bucket replication or object-level ACLs. A rebuildable cache uses
  # none of those.
  hierarchical_namespace {
    enabled = true
  }

  # No soft delete. It is on by default with a seven-day retention, and
  # soft-deleted objects keep accruing storage charges for the whole of it.
  #
  # That is the wrong default for this bucket specifically: gcsfuse renames
  # through copy-and-delete, and the lifecycle rule below deletes on a schedule,
  # so a cache generates deletions continuously. Retaining a week of them would
  # cost more than the cache itself on a bucket that turns over every 30 days -
  # to protect data that is, by construction, recomputable.
  soft_delete_policy {
    retention_duration_seconds = 0
  }

  # Nothing here is public and nothing should become public by accident. The
  # bucket holds model weights and compilation output for a CI fleet.
  public_access_prevention = "enforced"

  # The cache is rebuildable by definition - a lost entry costs a recompile,
  # not data - but rebuilding all of it costs about 2.7x a suite's chips, so
  # do not let a terraform mistake take it.
  force_destroy = false

  lifecycle_rule {
    condition {
      age = var.models_lifecycle_age_days
    }
    action {
      type = "Delete"
    }
  }

  # Resumable uploads that never finished. gcsfuse uploads large objects in
  # parts, and a pod evicted or killed mid-write leaves those parts behind,
  # billed as storage and invisible to an object listing.
  lifecycle_rule {
    condition {
      age = 1
    }
    action {
      type = "AbortIncompleteMultipartUpload"
    }
  }

  labels = merge(var.labels, {
    purpose = "tpu-ci-models"
    cluster = each.key
    region  = local.cache_region[each.key]
  })
}

# Bucket-scoped and additive on purpose.
#
# _member manages exactly one (bucket, role, member) tuple. _binding would own
# the whole role and _policy the whole bucket, either of which fights anything
# else that manages IAM here - terraform reverting their change, them reverting
# terraform's. Project-level grants are also avoided: they are reconciled away
# by internal tooling, where bucket-level ones persist.
resource "google_storage_bucket_iam_member" "cache_workload_identity_rw" {
  for_each = var.worker_clusters

  bucket = google_storage_bucket.cache[each.key].name
  role   = "roles/storage.objectUser"
  member = "serviceAccount:${each.value.project}.svc.id.goog[buildkite/tpu-workload]"
}

output "cache_buckets" {
  description = "Compilation cache and model buckets per worker cluster."
  value = {
    for k, v in var.worker_clusters : k => {
      cache  = google_storage_bucket.cache[k].name
      models = google_storage_bucket.models[k].name
      region = local.cache_region[k]
    }
  }
}

resource "google_storage_bucket_iam_member" "models_workload_identity_rw" {
  for_each = var.worker_clusters

  bucket = google_storage_bucket.models[each.key].name
  role   = "roles/storage.objectUser"
  member = "serviceAccount:${each.value.project}.svc.id.goog[buildkite/tpu-workload]"
}
