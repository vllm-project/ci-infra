---
# What the manager's Kueue controller may do in this worker cluster, reached
# over Connect Gateway as the manager's Workload Identity.
#
# MultiKueue's manager only mirrors objects: it creates the Job or JobSet here,
# watches it, copies status back, and deletes it when the workload finishes or
# is evicted. This grants that and nothing else, because the alternative is a
# controller in another project holding broad rights over a cluster that runs
# workload images built from pull requests.
#
# If dispatch breaks, the symptom is workloads admitted on the manager with no
# pods appearing here, and the Kueue controller logging a forbidden error
# naming the verb it wanted. Add that verb.
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: kueue-multikueue-remote
rules:
  # The mirrored workload objects themselves.
  - apiGroups: ["kueue.x-k8s.io"]
    resources: ["workloads"]
    verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
  - apiGroups: ["kueue.x-k8s.io"]
    resources: ["workloads/status", "workloads/finalizers"]
    verbs: ["get", "update", "patch"]
  # The two job kinds this fleet dispatches. Both are enabled in
  # worker-config.yaml and must match the manager's framework list.
  - apiGroups: ["batch"]
    resources: ["jobs"]
    verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
  - apiGroups: ["batch"]
    resources: ["jobs/status"]
    verbs: ["get"]
  - apiGroups: ["jobset.x-k8s.io"]
    resources: ["jobsets"]
    verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
  - apiGroups: ["jobset.x-k8s.io"]
    resources: ["jobsets/status"]
    verbs: ["get"]
  # Status reporting: why a workload is pending, and which pods it produced.
  - apiGroups: [""]
    resources: ["events"]
    verbs: ["get", "list", "watch", "create", "patch"]
  - apiGroups: [""]
    resources: ["pods"]
    verbs: ["get", "list", "watch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: kueue-multikueue-remote
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: kueue-multikueue-remote
subjects:
  # Connect Gateway presents a federated Workload Identity principal in this
  # form, so the binding is to a User rather than to a ServiceAccount in this
  # cluster - the manager's controller has no identity here.
  - kind: User
    name: serviceAccount:${PROJECT_ID}.svc.id.goog[kueue-system/kueue-controller-manager]
    apiGroup: rbac.authorization.k8s.io
