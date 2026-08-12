# Golden compilation cache

A read-only volume per worker cluster, populated once from
`gs://ullm-ci-cache/jax_cache` and mounted by every TPU workload.

## Why

Bare-metal CI keeps the JAX compilation cache on the VM's persistent disk and
syncs it to GCS around each run (`run_in_docker.sh`), so a test reads it at
local-disk speed and a second run of the same test is warm. A pod has no such
disk: it starts cold, recompiles, and the work is thrown away when it exits.

That matters more than it sounds. Compilation happens *inside* the test, so a
cold cache does not just add wall clock — it inflates the per-test numbers you
would compare against bare metal. Running these tests on Kubernetes is meant to
answer "is this platform equivalent?", and a cold cache makes that question
unanswerable.

Pointing `JAX_COMPILATION_CACHE_DIR` at `gs://` directly also works and needs no
volume at all. It is the right first step, and it is what `pod_entrypoint.sh` in
the tpu-inference repo does. But every lookup becomes a network round trip,
which is a different performance shape from bare metal. This volume reproduces
the bare-metal shape — local reads, shared content — for when that difference
turns out to matter.

## Prerequisite

Workload Identity is enforced on the worker clusters, so pods do **not** inherit
the node service account. A probe of the default one returned:

```
ACTIVE  ACCOUNT
*       cloud-tpu-inference-test.svc.id.goog
ERROR: HTTPError 403: Caller does not have storage.objects.get access to
       .../buckets/ullm-ci-cache/objects/jax_cache/
```

So nothing here works until a service account with bucket read access exists and
is bound to the populate job's Kubernetes service account. Both commands are in
the header of `provision_golden_cache.sh`. The bucket lives in the bare-metal CI
project, so this is a cross-project grant.

## Usage

```bash
./provision_golden_cache.sh \
  --cluster tpu-ci-southamerica-west1-a \
  --project cloud-tpu-inference-test \
  --gsa tpu-cache-reader@<bucket-project>.iam.gserviceaccount.com \
  --jax-version 0.4.35
```

Once per worker cluster. The volume does not follow a workload: MultiKueue
dispatches to whichever cluster has capacity, and a disk in one region is
invisible from another.

## What it does, and why it is four steps

1. **populate** — a ReadWriteOnce claim, filled from the bucket by a Job
2. **release** — delete the claim; the `Retain` policy leaves the disk behind
3. **rebind** — clear the `claimRef` and re-advertise the PV `ReadOnlyMany`
4. **publish** — a `ReadOnlyMany` claim every workload can mount at once

Steps 2–4 are GCE Persistent Disk semantics, not ceremony: a disk may be
attached read-only to many nodes, but only while nothing holds it read-write.
The writer has to let go before the readers can arrive.

## Things that will bite

**JAX version.** The cache namespace is `jax${JAX_VERSION}_tpu${TPU_VERSION}`,
matching bare metal exactly. A JAX bump silently starts from an empty cache —
rerun with the new `--jax-version` or the volume quietly stops helping.

**Zone.** A Persistent Disk is zonal. `WaitForFirstConsumer` binding exists so
the disk is created in the zone the populate pod landed in; if TPU node pools
later move zones, the volume is stranded and has to be rebuilt.

**Staleness.** This volume is a snapshot in time. New entries compiled by tests
go nowhere, because it is mounted read-only. Bare metal avoids that by pushing
to GCS on exit 0, and `pod_entrypoint.sh` does the same — so the bucket stays
the source of truth and this volume is a fast local copy of it. Re-run the
script periodically to refresh, or the gap widens as tests change.

**It is not free.** A retained PD per worker cluster, sized for the whole cache.
Weigh that against `gs://` directly, which costs nothing extra and may be fast
enough.
