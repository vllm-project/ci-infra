apiVersion: v1
kind: Namespace
metadata:
  name: ${NAMESPACE}
  labels:
    pod-security.kubernetes.io/enforce: baseline
    pod-security.kubernetes.io/audit: restricted
    pod-security.kubernetes.io/warn: restricted
---
apiVersion: external-secrets.io/v1
kind: ClusterSecretStore
metadata:
  name: gcp-secret-manager
spec:
  provider:
    gcpsm:
      projectID: "${SECRET_PROJECT}"
---
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: agent-stack-k8s-secret
  namespace: ${NAMESPACE}
spec:
  refreshInterval: 1h
  secretStoreRef:
    kind: ClusterSecretStore
    name: gcp-secret-manager
  target:
    name: agent-stack-k8s-secret
    creationPolicy: Owner
  data:
    - secretKey: BUILDKITE_AGENT_TOKEN
      remoteRef:
        key: ${SECRET_ID}
---
# Hugging Face token for workloads that pull models. Synced from the same
# Secret Manager entry the bare-metal agents read, so there is one place to
# rotate it. Workload manifests reference it with a secretKeyRef rather than
# having the token pass through the pipeline.
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: hf-token-secret
  namespace: ${NAMESPACE}
spec:
  refreshInterval: 1h
  secretStoreRef:
    kind: ClusterSecretStore
    name: gcp-secret-manager
  target:
    name: hf-token-secret
    creationPolicy: Owner
  data:
    - secretKey: HF_TOKEN
      remoteRef:
        key: ${HF_SECRET_ID}
