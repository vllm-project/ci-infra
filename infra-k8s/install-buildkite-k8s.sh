#!/usr/bin/env bash

set -euo pipefail

agentStackVersion=${AGENT_STACK_K8S_VERSION:-0.46.3}
scriptDir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

# gcloud container clusters get-credentials vllm-ci-test-cluster --region us-central1 --project vllm-405802

# ensure current K8s cluster is vllm-ci-test-cluster
if ! kubectl config current-context | grep -q vllm-ci-test-cluster; then
    echo "Current K8s cluster is not vllm-ci-test-cluster"
    exit 1
fi

agentToken=${TF_VAR_buildkite_agent_token:-}
if [ -z "$agentToken" ]; then
    echo "TF_VAR_buildkite_agent_token is not set"
    exit 1
fi

agentQueue=${BUILDKITE_AGENT_QUEUE:-}
if [ -z "$agentQueue" ]; then
    echo "BUILDKITE_AGENT_QUEUE is not set"
    exit 1
fi

helm upgrade --install agent-stack-k8s oci://ghcr.io/buildkite/helm/agent-stack-k8s \
    --version "$agentStackVersion" \
    --create-namespace \
    --namespace buildkite \
    --values "$scriptDir/agent-stack-values.yaml" \
    --set-string agentToken="$agentToken" \
    --set-string config.queue="$agentQueue" \
    --atomic \
    --wait \
    --timeout 10m
