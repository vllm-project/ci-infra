#!/bin/bash
# Phase 2 of docs/gpu-queues-eks-migration.md: secrets + agent-stack-k8s controller
# for the l4-k8s queue on the l4-ci EKS cluster.
#
#   BUILDKITE_AGENT_TOKEN=... HF_TOKEN=... BUILDKITE_ANALYTICS_TOKEN=... \
#     bash infra-k8s/install-buildkite-k8s-eks.sh
set -euo pipefail

kubectl config current-context | grep l4-ci || { echo "wrong context (want l4-ci)"; exit 1; }

: "${BUILDKITE_AGENT_TOKEN:?agent token for the ci Buildkite cluster}"
: "${HF_TOKEN:?required}"
: "${BUILDKITE_ANALYTICS_TOKEN:?required}"

CHART_VERSION=0.44.0   # >=0.28 needs no GraphQL token/org; latest at time of writing 0.49.0

kubectl create namespace buildkite --dry-run=client -o yaml | kubectl apply -f -

# Secrets consumed by the Phase 4 pod template (k8s_plugin.py).
kubectl -n buildkite create secret generic hf-token-secret \
  --from-literal=token="$HF_TOKEN" --dry-run=client -o yaml | kubectl apply -f -
kubectl -n buildkite create secret generic buildkite-analytics-token \
  --from-literal=token="$BUILDKITE_ANALYTICS_TOKEN" --dry-run=client -o yaml | kubectl apply -f -

# Job pods' service account (plain SA: Phase 0 item 5 found the tests use no AWS
# APIs; ECR pulls go through the node role).
kubectl -n buildkite create serviceaccount buildkite-gpu-jobs \
  --dry-run=client -o yaml | kubectl apply -f -

# NOTE: config.tags is a LIST in the chart schema — --set config.tags="queue=x"
# silently misconfigures; --set-json is required. max-in-flight defaults to 25,
# which would masquerade as an autoscaling failure; 0 = unlimited (node group max
# is the real cap). Do NOT set config.job-ttl: it hard-kills jobs at the TTL and
# distributed L4 tests legitimately run 20-40min (learned the hard way, ci#86133).
helm upgrade --install agent-stack-k8s-l4 \
  oci://ghcr.io/buildkite/helm/agent-stack-k8s \
  --version "${CHART_VERSION}" \
  --namespace buildkite \
  --set agentToken="$BUILDKITE_AGENT_TOKEN" \
  --set-json 'config.tags=["queue=l4-k8s"]' \
  --set config.max-in-flight=0

kubectl -n buildkite rollout status deployment/agent-stack-k8s-l4-controller --timeout=120s
