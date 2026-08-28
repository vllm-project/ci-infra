#!/usr/bin/env bash
#
# mkexec — attach to a pod for a job you submitted to the MultiKueue manager,
# without knowing (or having pre-configured) the worker cluster that ran it.
#
# The user only ever names the manager. Everything else is discovered:
#
#   Job -> Workload            (manager: ownerReferences)
#   Workload -> worker cluster (manager: .status.clusterName)
#   worker -> ClusterProfile   (manager: MultiKueueCluster.spec.clusterSource)
#   ClusterProfile -> endpoint (manager: .status.accessProviders[].cluster.server,
#                               a Connect Gateway URL published by the GKE fleet)
#
# This is an ordinary CLI - kubectl, python3, and gcloud application-default
# credentials. Nothing in it needs to run inside a cluster, so a workstation or
# a plain VM with gcloud configured behaves identically.
#
# Auth is the caller's own credentials through gke-gcloud-auth-plugin against
# the gateway URL: no shared worker kubeconfig is handed out, and what you may
# do on the worker is whatever your own IAM and RBAC allow. Those grants are
# NOT provisioned by this repo's terraform - see "Attaching to a running
# workload" in ../README.md for what an operator has to be given first.
#
# Usage:
#   mkexec.sh <job-name> [-n namespace] [-c container] [-i index] [-- cmd ...]
#
# Env:
#   MK_MANAGER_CONTEXT  kubectl context for the manager cluster
#   MK_NAMESPACE        default namespace (default: buildkite)

set -euo pipefail

MANAGER_CONTEXT="${MK_MANAGER_CONTEXT:-gke_cloud-ullm-inference-ci-cd_us-central1_tpu-ci-manager}"
NAMESPACE="${MK_NAMESPACE:-buildkite}"
CONTAINER=""
POD_INDEX=0
JOB=""
CMD=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    -n|--namespace) NAMESPACE="$2"; shift 2 ;;
    -c|--container) CONTAINER="$2"; shift 2 ;;
    -i|--index)     POD_INDEX="$2"; shift 2 ;;
    --context)      MANAGER_CONTEXT="$2"; shift 2 ;;
    --)             shift; CMD=("$@"); break ;;
    -h|--help)      sed -n '2,30p' "$0"; exit 0 ;;
    -*)             echo "unknown flag: $1" >&2; exit 2 ;;
    *)              JOB="$1"; shift ;;
  esac
done

[[ -n "$JOB" ]] || { echo "usage: $(basename "$0") <job-name> [-n ns] [-c container] [-- cmd ...]" >&2; exit 2; }
[[ ${#CMD[@]} -gt 0 ]] || CMD=(/bin/sh -c 'exec /bin/bash || exec /bin/sh')

mgr() { kubectl --context="$MANAGER_CONTEXT" --request-timeout=30s "$@"; }
note() { printf '\033[2m%s\033[0m\n' "$*" >&2; }

# 1+2. Job -> Workload -> the worker cluster MultiKueue dispatched it to.
#
# Kueue names workloads unpredictably, so the Job is matched on ownership
# rather than on a derived name. The winning cluster is read from the typed
# .status.clusterName, which MultiKueue sets once and never changes; the
# admission check message is only consulted as a fallback, because it is
# human-readable prose that Kueue is free to reword between releases.
note "resolving workload for job/$JOB in $NAMESPACE on $MANAGER_CONTEXT ..."
set +e
WORKER="$(mgr -n "$NAMESPACE" get workloads -o json | python3 -c '
import json, re, sys

job = sys.argv[1]
wl = None
for w in json.load(sys.stdin)["items"]:
    for o in w["metadata"].get("ownerReferences", []):
        if o["name"] == job and o["kind"] in ("Job", "JobSet", "RayJob", "MPIJob"):
            wl = w
            break
    if wl:
        break

if wl is None:
    sys.exit(2)

status = wl.get("status", {})
cluster = status.get("clusterName")
if not cluster:
    for check in status.get("admissionChecks", []):
        m = re.search(r"\"([^\"]+)\"", check.get("message") or "")
        if m:
            cluster = m.group(1)
            break

if not cluster:
    nominated = status.get("nominatedClusterNames") or []
    if nominated:
        print("  nominated, not yet admitted: " + ", ".join(nominated), file=sys.stderr)
    for c in status.get("conditions", []):
        print("  %s=%s: %s" % (c.get("type"), c.get("status"), c.get("message", "")),
              file=sys.stderr)
    sys.exit(3)

print(cluster)
' "$JOB")"
rc=$?
set -e
case "$rc" in
  0) ;;
  2) echo "no Workload owned by '$JOB' in namespace $NAMESPACE on the manager." >&2; exit 1 ;;
  3) echo "workload for '$JOB' is not dispatched to a worker yet (still queued?)." >&2; exit 1 ;;
  *) echo "could not read workloads from the manager." >&2; exit 1 ;;
esac
note "dispatched to worker cluster: $WORKER"

# 3+4. MultiKueueCluster -> ClusterProfile -> Connect Gateway endpoint.
#      Several ClusterProfiles can share a name across namespaces; only the one
#      reconciled by the fleet controller carries a populated status.
PROFILE_REF="$(mgr get multikueuecluster "$WORKER" -o jsonpath='{.spec.clusterSource.clusterProfileRef.name}')"
[[ -n "$PROFILE_REF" ]] || { echo "MultiKueueCluster/$WORKER has no clusterProfileRef" >&2; exit 1; }

SERVER="$(mgr get clusterprofile -A -o json | python3 -c '
import json, sys
ref = sys.argv[1]
for p in json.load(sys.stdin)["items"]:
    if p["metadata"]["name"] != ref:
        continue
    for ap in (p.get("status") or {}).get("accessProviders", []):
        server = (ap.get("cluster") or {}).get("server")
        if server:
            print(server); sys.exit(0)
sys.exit(1)
' "$PROFILE_REF")" || { echo "no access endpoint published for ClusterProfile/$PROFILE_REF" >&2; exit 1; }
note "endpoint: $SERVER"

# 5. Synthesize a throwaway kubeconfig for that endpoint, authenticated as the
#    caller. Nothing is written to the user's real kubeconfig.
KUBECONFIG_TMP="$(mktemp -t mkexec-kubeconfig)"
trap 'rm -f "$KUBECONFIG_TMP"' EXIT
cat >"$KUBECONFIG_TMP" <<EOF
apiVersion: v1
kind: Config
current-context: worker
clusters:
- name: worker
  cluster:
    server: ${SERVER}
users:
- name: caller
  user:
    exec:
      apiVersion: client.authentication.k8s.io/v1beta1
      command: gke-gcloud-auth-plugin
      args: ["--use_application_default_credentials"]
      provideClusterInfo: true
      interactiveMode: IfAvailable
contexts:
- name: worker
  context:
    cluster: worker
    user: caller
    namespace: ${NAMESPACE}
EOF

wrk() { kubectl --kubeconfig="$KUBECONFIG_TMP" --request-timeout=30s "$@"; }

# 6. Check the caller may actually exec before hunting for a pod, so the common
#    failure - being able to resolve the cluster but not having been granted
#    access to it - reports what is missing instead of a bare 403. Only an
#    explicit "no" is fatal: if the check itself cannot run, fall through and
#    let the real request produce the real error.
if [[ "$(wrk auth can-i create pods/exec -n "$NAMESPACE" 2>/dev/null || true)" == "no" ]]; then
  cat >&2 <<EOF
$(gcloud config get-value account 2>/dev/null) cannot exec in $NAMESPACE on $WORKER.

Reaching a worker pod needs two grants, neither of which this repo provisions:

  1. GCP IAM. exec is a create on pods/exec, a write verb, so gatewayReader is
     not enough:
       roles/gkehub.gatewayEditor  on the worker project
       roles/gkehub.viewer         on the manager project (to resolve memberships)

  2. Kubernetes RBAC on the worker, binding your identity to pods, pods/log and
     pods/exec. Over Connect Gateway the subject is your email, not a
     ServiceAccount - see kueue/templates/launcher_rbac_worker.yaml.tpl for the
     shape, and ../README.md for a ready-to-apply group binding.
EOF
  exit 1
fi

# 7. Find the pod on the worker. Plain Jobs and JobSets label their pods
#    differently. Names are sorted rather than taken in list order, so -i is
#    stable: a JobSet's pods carry their job and completion indices in the
#    name, and index 0 is the slice's worker 0.
POD=""
for selector in "batch.kubernetes.io/job-name=$JOB" "jobset.sigs.k8s.io/jobset-name=$JOB"; do
  POD="$(wrk -n "$NAMESPACE" get pods -l "$selector" \
    --field-selector=status.phase=Running \
    -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null \
    | sort | sed -n "$((POD_INDEX + 1))p")"
  [[ -n "$POD" ]] && break
done
[[ -n "$POD" ]] || { echo "no Running pod for '$JOB' on $WORKER (still pending or already finished?)" >&2; exit 1; }

note "exec into $WORKER/$NAMESPACE/$POD"
exec_args=(-n "$NAMESPACE" exec -it "$POD")
[[ -n "$CONTAINER" ]] && exec_args+=(-c "$CONTAINER")
wrk "${exec_args[@]}" -- "${CMD[@]}"
