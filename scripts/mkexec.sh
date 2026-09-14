#!/usr/bin/env bash
#
# mkexec — attach to a pod for a job you submitted to the MultiKueue manager,
# without knowing (or having pre-configured) the worker cluster that ran it.
#
# The user only ever names the manager. Everything else is discovered:
#
#   Job -> Workload            (manager: ownerReferences)
#   Workload -> worker cluster (manager: MultiKueue admission check state)
#   worker -> ClusterProfile   (manager: MultiKueueCluster.spec.clusterSource)
#   ClusterProfile -> endpoint (manager: .status.accessProviders[].cluster.server,
#                               a Connect Gateway URL published by the GKE fleet)
#
# Auth is the caller's own gcloud ADC via gke-gcloud-auth-plugin against that
# gateway URL — no shared worker kubeconfig is handed out.
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
    -h|--help)      sed -n '2,25p' "$0"; exit 0 ;;
    -*)             echo "unknown flag: $1" >&2; exit 2 ;;
    *)              JOB="$1"; shift ;;
  esac
done

[[ -n "$JOB" ]] || { echo "usage: $(basename "$0") <job-name> [-n ns] [-c container] [-- cmd ...]" >&2; exit 2; }
[[ ${#CMD[@]} -gt 0 ]] || CMD=(/bin/sh -c 'exec /bin/bash || exec /bin/sh')

mgr() { kubectl --context="$MANAGER_CONTEXT" --request-timeout=30s "$@"; }
note() { printf '\033[2m%s\033[0m\n' "$*" >&2; }

# 1. Job -> Workload. Kueue names workloads unpredictably, so match on ownership.
note "resolving workload for job/$JOB in $NAMESPACE on $MANAGER_CONTEXT ..."
WORKLOAD_JSON="$(mgr -n "$NAMESPACE" get workloads -o json | python3 -c '
import json, sys
job = sys.argv[1]
for w in json.load(sys.stdin)["items"]:
    for o in w["metadata"].get("ownerReferences", []):
        if o["name"] == job and o["kind"] in ("Job", "JobSet", "RayJob", "MPIJob"):
            json.dump(w, sys.stdout); sys.exit(0)
sys.exit(1)
' "$JOB")" || { echo "no Workload owned by '$JOB' in namespace $NAMESPACE" >&2; exit 1; }

# 2. Workload -> the worker cluster MultiKueue dispatched it to. Kueue records
#    this in the admission check message as: ... on "<MultiKueueCluster name>".
WORKER="$(python3 -c '
import json, re, sys
wl = json.load(sys.stdin)
for c in wl.get("status", {}).get("admissionChecks", []):
    m = re.search(r"\"([^\"]+)\"", c.get("message", "") or "")
    if m:
        print(m.group(1)); sys.exit(0)
sys.exit(1)
' <<<"$WORKLOAD_JSON")" || {
  echo "workload not dispatched yet (no MultiKueue admission check message)." >&2
  python3 -c '
import json,sys
wl=json.load(sys.stdin)
for c in wl.get("status",{}).get("conditions",[]):
    print(f"  {c[\"type\"]}={c[\"status\"]}: {c.get(\"message\",\"\")}")
' <<<"$WORKLOAD_JSON" >&2
  exit 1
}
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

# 6. Find the pod on the worker. Plain Jobs and JobSets label their pods differently.
POD=""
for selector in "batch.kubernetes.io/job-name=$JOB" "jobset.sigs.k8s.io/jobset-name=$JOB"; do
  POD="$(wrk -n "$NAMESPACE" get pods -l "$selector" \
    --field-selector=status.phase=Running \
    -o jsonpath="{.items[$POD_INDEX].metadata.name}" 2>/dev/null || true)"
  [[ -n "$POD" ]] && break
done
[[ -n "$POD" ]] || { echo "no Running pod for '$JOB' on $WORKER (still pending or already finished?)" >&2; exit 1; }

note "exec into $WORKER/$NAMESPACE/$POD"
exec_args=(-n "$NAMESPACE" exec -it "$POD")
[[ -n "$CONTAINER" ]] && exec_args+=(-c "$CONTAINER")
wrk "${exec_args[@]}" -- "${CMD[@]}"
