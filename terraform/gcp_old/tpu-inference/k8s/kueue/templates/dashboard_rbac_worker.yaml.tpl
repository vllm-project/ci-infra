---
# What the health dashboard may read on a worker: events in the fleet's
# namespace, and nothing else. A TPU pod's scheduling failures, scale-ups,
# failed creates and failure-policy kills are recorded here, where the pod runs,
# not on the manager. Everything else the dashboard reports comes from the
# manager; see dashboard_rbac.yaml.tpl.
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
    resources: ["events"]
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
