# Golden compilation cache

A VolumeSnapshot per worker cluster, taken once from a disk populated out of
`gs://ullm-ci-cache/jax_cache`. Every test clones its own writable volume from
it.

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

## What it does

1. **populate** — a ReadWriteOnce claim, filled from the bucket by a Job
2. **snapshot** — a VolumeSnapshot of it; this is the golden artifact
3. **release** — delete the claim and the Job; the snapshot stands alone

## How tests consume it

Not by mounting it. `manifests/test.yaml` in tpu-inference declares a **generic
ephemeral volume** whose `dataSource` is this snapshot, so each pod gets a
private writable clone that is created with it and deleted with it.

Two problems fall away as a result. The PVC is created by the controller in
whichever cluster the pod was dispatched to, so MultiKueue copying only the Job
stops mattering. And the clone is writable, so a test can write the entries it
compiles — a shared read-only cache would have left every test recompiling
whatever the golden copy lacked, forever.

`pod_entrypoint.sh` then pushes those new entries to GCS on success, so the
bucket stays the source of truth and the snapshot is a fast local head start.
That is the same division of labour bare metal has between GCS and its own
persistent disk.

## Things that will bite

**JAX version.** The cache namespace is `jax${JAX_VERSION}_tpu${TPU_VERSION}`,
matching bare metal exactly. A JAX bump silently starts from an empty cache —
rerun with the new `--jax-version` or the volume quietly stops helping.

**Zone.** A Persistent Disk is zonal. `WaitForFirstConsumer` binding exists so
the disk is created in the zone the populate pod landed in; if TPU node pools
later move zones, the volume is stranded and has to be rebuilt.

**Staleness.** The snapshot is a point in time and never updates itself. New
entries do reach GCS via `pod_entrypoint.sh`, so nothing is lost — but the head
start decays as tests change, and re-running this script is what refreshes it.

**Chips idle while the clone provisions.** The volume is created after Kueue
admits the workload, so the pod already holds its chips while the disk is
hydrated from the snapshot. On a 10-chip cohort that is the most expensive
place in the system to add latency, and it is the number to watch when judging
whether this beats pointing JAX at `gs://` directly.

**It is not free.** A retained snapshot per worker cluster, plus a short-lived
disk per test. Weigh that against `gs://` directly, which costs nothing extra
and may be fast enough.
