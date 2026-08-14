#!/usr/bin/env bash

set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "usage: $0 <kube-context> <namespace>" >&2
  exit 2
fi

if [[ -z "${BUILDKITE_ANALYTICS_TOKEN:-}" ]]; then
  echo "BUILDKITE_ANALYTICS_TOKEN is not set." >&2
  exit 1
fi

context=$1
namespace=$2

# Stream the token through stdin so it is not exposed in the process list.
printf '%s' "$BUILDKITE_ANALYTICS_TOKEN" \
  | kubectl --context "$context" --namespace "$namespace" create secret generic \
      buildkite-analytics-token-secret \
      --from-file=token=/dev/stdin \
      --dry-run=client \
      -o yaml \
  | kubectl --context "$context" --namespace "$namespace" apply -f -
