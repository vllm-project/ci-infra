---
# What the health dashboard may read on a worker: events and pods in the
# fleet's namespace, and nothing else. A TPU pod's scheduling failures,
# scale-ups, failed creates and failure-policy kills are recorded here, where
# the pod runs, not on the manager. So is the flavor the pod really got: the
# worker's Kueue admits a workload again and picks its own, which is what puts
# a pod on a Spot node or not. Everything else the dashboard reports comes from
# the manager; see dashboard_rbac.yaml.tpl.
#
# The subject is the dashboard's Google service account, which reaches this
# cluster through Connect Gateway.
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: tpu-ci-dashboard
  namespace: ${NAMESPACE}
rules:
  - apiGroups: [""]
    resources: ["events", "pods"]
    verbs: ["get", "list"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: tpu-ci-dashboard
  namespace: ${NAMESPACE}
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: tpu-ci-dashboard
subjects:
  - kind: User
    name: ${DASHBOARD_SERVICE_ACCOUNT}
    apiGroup: rbac.authorization.k8s.io
