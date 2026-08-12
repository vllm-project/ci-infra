#!/usr/bin/env bash
# Provision the golden JAX compilation cache volume in one worker cluster.
#
#   ./provision_golden_cache.sh --cluster tpu-ci-southamerica-west1-a \
#       --project cloud-tpu-inference-test \
#       --gsa tpu-cache-reader@<bucket-project>.iam.gserviceaccount.com
#
# Run once per worker cluster, and again whenever the JAX version changes - the
# cache namespace is keyed on it, so a bump silently starts from empty.
#
# WHY A VOLUME AND NOT JUST THE BUCKET
#
# Bare metal keeps the cache on the VM's persistent disk and syncs it to GCS
# around each run, so a test reads it at local-disk speed. A pod has no such
# disk. Pointing JAX straight at gs:// works but turns every cache lookup into
# a network round trip, which is a different performance profile from bare
# metal - and the point of running these tests here is to compare the two.
# A read-only volume, populated once from the same bucket, reproduces the
# bare-metal shape: local reads, shared content.
#
# WHAT IT DOES
#
#   1. populate  a ReadWriteOnce claim, filled from the bucket by a Job
#   2. release   delete the claim; Retain leaves the disk behind
#   3. rebind    clear the claimRef and re-advertise the PV ReadOnlyMany
#   4. publish   a ReadOnlyMany claim every workload can mount at once
#
# Steps 2-4 are the price of GCE Persistent Disk semantics: a disk may be
# attached read-only to many nodes, but only while nothing holds it read-write.
# So the writer has to let go before the readers can arrive.
#
# PREREQUISITE, and currently the blocker: the service account passed as --gsa
# must be able to read the cache bucket. Workload Identity is enforced on the
# worker clusters, so pods do not inherit the node service account - a probe of
# the default one came back 403 on gs://ullm-ci-cache. Grant it with:
#
#   gcloud storage buckets add-iam-policy-binding gs://ullm-ci-cache \
#     --member="serviceAccount:${GSA}" --role=roles/storage.objectViewer
#
# and bind the Kubernetes side:
#
#   gcloud iam service-accounts add-iam-policy-binding "${GSA}" \
#     --role=roles/iam.workloadIdentityUser \
#     --member="serviceAccount:${PROJECT}.svc.id.goog[${NAMESPACE}/tpu-cache-populate]"
set -euo pipefail

CLUSTER=""
PROJECT="${PROJECT:-}"
GSA=""
NAMESPACE="${NAMESPACE:-buildkite}"
CACHE_SIZE="${CACHE_SIZE:-200Gi}"
GCS_CACHE_BASE="${GCS_CACHE_BASE:-gs://ullm-ci-cache/jax_cache}"
JAX_VERSION="${JAX_VERSION:-}"
TPU_VERSION="${TPU_VERSION:-tpu6e}"
GOLDEN_CLAIM="${GOLDEN_CLAIM:-tpu-cache-golden}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --cluster)      CLUSTER="$2"; shift 2 ;;
    --project)      PROJECT="$2"; shift 2 ;;
    --gsa)          GSA="$2"; shift 2 ;;
    --namespace)    NAMESPACE="$2"; shift 2 ;;
    --size)         CACHE_SIZE="$2"; shift 2 ;;
    --jax-version)  JAX_VERSION="$2"; shift 2 ;;
    --tpu-version)  TPU_VERSION="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

for required in CLUSTER PROJECT GSA JAX_VERSION; do
  if [[ -z "${!required}" ]]; then
    echo "--${required,,} is required" >&2
    exit 2
  fi
done

# Must match run_in_docker.sh exactly, or the two platforms read different
# caches and the comparison they exist to support is meaningless.
CACHE_NAMESPACE="jax${JAX_VERSION}_tpu${TPU_VERSION}"
GCS_CACHE_PATH="${GCS_CACHE_BASE}/${CACHE_NAMESPACE}"

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
kube() { kubectl --context "connectgateway_${PROJECT}_${CLUSTER}" "$@"; }

echo "==> credentials for ${CLUSTER}"
gcloud container fleet memberships get-credentials "$CLUSTER" --project "$PROJECT"

if kube -n "$NAMESPACE" get pvc "$GOLDEN_CLAIM" >/dev/null 2>&1; then
  echo "==> ${GOLDEN_CLAIM} already exists in ${CLUSTER}; nothing to do."
  echo "    Delete it and its PV to rebuild from ${GCS_CACHE_PATH}."
  exit 0
fi

echo "==> 1/4 populate from ${GCS_CACHE_PATH}"
NAMESPACE="$NAMESPACE" CACHE_SIZE="$CACHE_SIZE" CACHE_GSA="$GSA" \
  GCS_CACHE_PATH="$GCS_CACHE_PATH" \
  envsubst < "${here}/golden-cache.yaml" | kube apply -f -

# The claim binds only once the pod is scheduled (WaitForFirstConsumer), so
# wait on the Job rather than on the claim.
kube -n "$NAMESPACE" wait --for=condition=complete --timeout=60m job/tpu-cache-populate
kube -n "$NAMESPACE" logs job/tpu-cache-populate --tail=5

PV="$(kube -n "$NAMESPACE" get pvc tpu-cache-populate -o jsonpath='{.spec.volumeName}')"
echo "==> populated ${PV}"

echo "==> 2/4 release the writer"
kube -n "$NAMESPACE" delete job tpu-cache-populate --wait=true
kube -n "$NAMESPACE" delete pvc tpu-cache-populate --wait=true

echo "==> 3/4 make ${PV} available read-only to many nodes"
# Released still carries the old claimRef, which blocks rebinding; and the PV
# advertises only the access mode its first claim asked for.
kube patch pv "$PV" --type=merge -p '{"spec":{"claimRef":null}}'
kube patch pv "$PV" --type=merge \
  -p '{"spec":{"accessModes":["ReadOnlyMany"]}}'

echo "==> 4/4 publish ${GOLDEN_CLAIM}"
cat <<EOF | kube apply -f -
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: ${GOLDEN_CLAIM}
  namespace: ${NAMESPACE}
  labels:
    cache-namespace: "${CACHE_NAMESPACE}"
spec:
  accessModes: ["ReadOnlyMany"]
  storageClassName: tpu-cache-retain
  volumeName: ${PV}
  resources:
    requests:
      storage: ${CACHE_SIZE}
EOF

kube -n "$NAMESPACE" get pvc "$GOLDEN_CLAIM"
echo
echo "Done. Workloads in ${CLUSTER} can now mount ${GOLDEN_CLAIM} read-only."
echo "Cache namespace: ${CACHE_NAMESPACE} - rerun with a new --jax-version after a JAX bump."
