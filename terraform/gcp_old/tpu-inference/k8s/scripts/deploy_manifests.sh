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

# kubectl reads a directory in lexical order, which is what the NN- prefixes
# encode: 01-base carries the Namespace that everything else is created into.
# The rest is soft ordering - a ClusterQueue naming a ResourceFlavor that does
# not exist yet goes inactive and recovers when it appears - so applying the
# directory in one call is equivalent to applying the files in sequence.
kubectl apply -f "${GENERATED_DIR}/manager"
kubectl patch deployment kueue-controller-manager -n kueue-system --patch-file "${K8S_DIR}/kueue/manager-auth-plugin-patch.yaml"

echo ""
echo "========================================================="
echo "Deploying Kubernetes Manifests to Worker Clusters"
echo "========================================================="
for worker_dir in "${GENERATED_DIR}"/worker-*/; do
  [[ -d "${worker_dir}" ]] || continue
  worker_key=$(basename "${worker_dir}" | sed -e 's/^worker-//')
  echo "Applying manifests for worker: ${worker_key}"

  gcloud container clusters get-credentials "tpu-ci-${worker_key}" \
    --location "${worker_key}" \
    --project cloud-tpu-inference-test

  kubectl apply -f "${worker_dir}"
done

echo ""
echo "Deployment Complete!"
