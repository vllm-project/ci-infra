---
# Lets the launcher read workload pod logs in this worker cluster.
#
# The launcher runs on the manager, but MultiKueue creates the pods only here,
# so a step's output would never reach its Buildkite log otherwise. It arrives
# over Connect Gateway, which authenticates it as the Google principal behind
# its Kubernetes service account - hence a User subject, not a ServiceAccount.
#
# Cluster-scoped because the launcher follows the workload into whichever
# namespace MultiKueue mirrored it to. None of the verbs writes.
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: tpu-launcher-log-reader
rules:
  - apiGroups: [""]
    resources: ["pods", "pods/log"]
    verbs: ["get", "list", "watch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: tpu-launcher-log-reader
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: tpu-launcher-log-reader
subjects:
  # The manager's Workload Identity pool, not this cluster's: that is the
  # project federating the identity the launcher arrives as.
  - kind: User
    name: serviceAccount:${PROJECT_ID}.svc.id.goog[${NAMESPACE}/tpu-launcher]
    apiGroup: rbac.authorization.k8s.io
