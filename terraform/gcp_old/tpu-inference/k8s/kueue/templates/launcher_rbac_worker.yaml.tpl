---
# Lets the launcher read workload pod logs in this worker cluster.
#
# The launcher runs on the manager, but MultiKueue skips pod creation there -
# the pods only exist here - so `kubectl logs` against the manager returns
# nothing. It reaches this cluster over Connect Gateway, which authenticates it
# as its Google service account, so the RBAC subject is that account's email
# rather than a Kubernetes ServiceAccount.
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
  - kind: User
    name: serviceAccount:${PROJECT_ID}.svc.id.goog[${NAMESPACE}/tpu-launcher]
    apiGroup: rbac.authorization.k8s.io
