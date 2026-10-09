---
# Keeps ${DEPLOYMENT} answering while a node goes away or a pod is pushed off
# one. With a single replica at no priority, the kube-dns replicas GKE adds as
# TPU nodes join preempted it off the small system nodes several times a day,
# and every time its webhook had no endpoint: Kueue's rejects the launcher's
# job outright, JobSet's stalls every multi-host workload being created.
#
# GKE admits pods at system-cluster-critical only in a namespace whose
# ResourceQuota names that class; kube-system and GKE's own namespaces carry
# such a quota, and this is the same for ours. Four pods: two replicas, the
# surge pod of a rollout, and one spare.
apiVersion: v1
kind: ResourceQuota
metadata:
  name: critical-pods
  namespace: ${NAMESPACE}
spec:
  hard:
    pods: "4"
  scopeSelector:
    matchExpressions:
      - scopeName: PriorityClass
        operator: In
        values: ["system-cluster-critical"]
---
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: ${DEPLOYMENT}
  namespace: ${NAMESPACE}
spec:
  minAvailable: 1
  selector:
    matchLabels:
${SELECTOR}
---
# A partial object, applied server-side under its own field manager after the
# upstream release, so it owns these fields and nothing else. Upstream's
# replicas: 1 is taken out of the release at apply time (see
# deploy_manifests.py), or every deploy would scale the controller down and
# back up.
#
# Two replicas under leader election: one reconciles, both serve the webhook,
# and a lost leader is replaced within the 15 s lease. The class matches
# kube-dns's, so kube-dns can no longer preempt it and gets a new system node
# instead.
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ${DEPLOYMENT}
  namespace: ${NAMESPACE}
spec:
  replicas: 2
  template:
    spec:
      priorityClassName: system-cluster-critical
      affinity:
        podAntiAffinity:
          # Preferred, not required: a cluster with one system node still runs
          # both replicas, just without the protection from losing that node.
          preferredDuringSchedulingIgnoredDuringExecution:
            - weight: 100
              podAffinityTerm:
                topologyKey: kubernetes.io/hostname
                labelSelector:
                  matchLabels:
${POD_SELECTOR}
