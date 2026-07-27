#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
K8S_DIR="$(dirname "$SCRIPT_DIR")"
GENERATED_DIR="${K8S_DIR}/generated"

if [[ ! -d "${GENERATED_DIR}" ]]; then
  echo "Error: ${GENERATED_DIR} does not exist. Run python3 scripts/generate_manifests.py first."
  exit 1
fi

echo "========================================================="
echo "Deploying Kubernetes Manifests to Manager Cluster"
echo "========================================================="
gcloud container clusters get-credentials tpu-ci-manager \
  --location us-central1 \
  --project cloud-ullm-inference-ci-cd

if [[ -d "${GENERATED_DIR}/manager" ]]; then
  kubectl apply -f "${GENERATED_DIR}/manager"
else
  kubectl apply -f "${GENERATED_DIR}/manager.yaml"
fi
kubectl patch deployment kueue-controller-manager -n kueue-system --patch-file "${K8S_DIR}/kueue/manager-auth-plugin-patch.yaml"

echo ""
echo "========================================================="
echo "Deploying Kubernetes Manifests to Worker Clusters"
echo "========================================================="
for worker_file in "${GENERATED_DIR}"/worker-*.yaml; do
  if [[ -f "${worker_file}" ]]; then
    worker_key=$(basename "${worker_file}" | sed -e 's/worker-//' -e 's/\.yaml//')
    echo "Applying manifests for worker: ${worker_key}"
    
    gcloud container clusters get-credentials "tpu-ci-${worker_key}" \
      --location "${worker_key}" \
      --project cloud-tpu-inference-test
      
    if [[ -d "${GENERATED_DIR}/worker-${worker_key}" ]]; then
      kubectl apply -f "${GENERATED_DIR}/worker-${worker_key}"
    else
      kubectl apply -f "${worker_file}"
    fi
  fi
done

echo ""
echo "Deployment Complete!"
