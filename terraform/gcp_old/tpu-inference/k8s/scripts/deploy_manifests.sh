#!/usr/bin/env bash
set -euo pipefail

# Applies generated/ to the manager and every worker cluster.
#
#   deploy_manifests.sh [--yes]
#
# What is applied is the committed generated/, never a fresh render: those
# manifests went through review, and a shared cluster should only ever see what
# someone read in a PR. The generator still runs - into a scratch directory,
# purely to prove the committed output is up to date with prod.auto.tfvars.
#
# Note this only ever creates and updates. A queue deleted from the tfvars
# disappears from generated/ but stays in the cluster until someone removes it
# by hand.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
K8S_DIR="$(dirname "$SCRIPT_DIR")"
GENERATED_DIR="${K8S_DIR}/generated"

# The generator imports hcl2 and yaml. Point PYTHON at the interpreter that has
# them - a virtualenv's - if your python3 does not.
PYTHON="${PYTHON:-python3}"

MANAGER_CLUSTER="tpu-ci-manager"
MANAGER_LOCATION="us-central1"
MANAGER_PROJECT="cloud-ullm-inference-ci-cd"
WORKER_PROJECT="cloud-tpu-inference-test"

ASSUME_YES=0
for arg in "$@"; do
  case "$arg" in
    -y|--yes) ASSUME_YES=1 ;;
    *) echo "usage: ${0##*/} [--yes]" >&2; exit 2 ;;
  esac
done

# Deploy gets a kubeconfig of its own. get-credentials writes to whatever
# KUBECONFIG names and repoints its current-context, so sharing yours would
# leave your kubectl aimed at the last worker this touched.
TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "${TMP_ROOT}"' EXIT
export KUBECONFIG="${TMP_ROOT}/kubeconfig"

# Set by use_cluster, read by apply_dir. Every kubectl call names it explicitly
# rather than trusting the current context, so a get-credentials that failed
# cannot send the manager's manifests to a worker.
KCTX=""

use_cluster() {
  local cluster="$1" location="$2" project="$3"
  gcloud container clusters get-credentials "$cluster" \
    --location "$location" --project "$project"
  KCTX="$(kubectl config current-context)"
}

confirm() {
  (( ASSUME_YES )) && return 0
  if [[ ! -t 0 ]]; then
    echo "Error: nothing to read a confirmation from; pass --yes to apply unattended." >&2
    exit 1
  fi
  local reply
  read -r -p "$1 [y/N] " reply
  case "$reply" in
    [yY]|[yY][eE][sS]) return 0 ;;
    *) echo "Aborted."; exit 1 ;;
  esac
}

apply_dir() {
  local dir="$1" label="$2"

  echo ""
  echo "--- ${label}: pending changes ---"
  # kubectl diff exits 1 for "there are differences" and >1 for a real failure.
  # A CRD the cluster does not have yet is the >1 case, which is what a
  # first-ever deploy into an empty cluster looks like.
  local rc=0
  kubectl --context "$KCTX" diff -f "$dir" || rc=$?
  case "$rc" in
    0) echo "(no changes)"; return 0 ;;
    1) ;;
    *) echo "Error: could not diff against ${label}." >&2; exit 1 ;;
  esac

  confirm "Apply the above to ${label}?"
  # kubectl reads a directory in lexical order, which is what the NN- prefixes
  # encode: 01-base carries the Namespace that everything else is created into.
  # The rest is soft ordering - a ClusterQueue naming a ResourceFlavor that does
  # not exist yet goes inactive and recovers when it appears - so applying the
  # directory in one call is equivalent to applying the files in sequence.
  kubectl --context "$KCTX" apply -f "$dir"
}

if [[ ! -d "${GENERATED_DIR}" ]]; then
  echo "Error: ${GENERATED_DIR} does not exist. Run python3 scripts/generate_manifests.py first." >&2
  exit 1
fi

echo "Checking generated/ is up to date with prod.auto.tfvars"
if ! "${PYTHON}" "${SCRIPT_DIR}/generate_manifests.py" --out-dir "${TMP_ROOT}/fresh" >/dev/null; then
  echo "Error: could not render the manifests with '${PYTHON}' (see above)." >&2
  echo "Set PYTHON to an interpreter with hcl2 and pyyaml installed." >&2
  exit 1
fi
if ! diff -ruN --label committed "${GENERATED_DIR}" --label fresh "${TMP_ROOT}/fresh"; then
  echo "" >&2
  echo "Error: generated/ does not match what prod.auto.tfvars renders (diff above)." >&2
  echo "Run 'python3 scripts/generate_manifests.py' and commit the result, so what" >&2
  echo "reaches the cluster is what was reviewed." >&2
  exit 1
fi

echo "========================================================="
echo "Manager Cluster"
echo "========================================================="
use_cluster "${MANAGER_CLUSTER}" "${MANAGER_LOCATION}" "${MANAGER_PROJECT}"
apply_dir "${GENERATED_DIR}/manager" "manager"

for worker_dir in "${GENERATED_DIR}"/worker-*/; do
  [[ -d "${worker_dir}" ]] || continue
  worker_key=$(basename "${worker_dir}" | sed -e 's/^worker-//')

  echo ""
  echo "========================================================="
  echo "Worker Cluster: ${worker_key}"
  echo "========================================================="
  # The directory is named for the cluster's location, so it is both the
  # --location and the suffix of the cluster name.
  use_cluster "tpu-ci-${worker_key}" "${worker_key}" "${WORKER_PROJECT}"
  apply_dir "${worker_dir}" "worker ${worker_key}"
done

echo ""
echo "Deployment Complete!"
