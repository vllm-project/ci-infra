---
# A worker the manager may dispatch to. The credentials come from a
# ClusterProfile that GKE's fleet writes into kueue-system by itself, because
# the manager cluster carries the labels fleet-clusterinventory-management-
# cluster=true and fleet-clusterinventory-namespace=kueue-system. Nothing here
# holds a token for the worker.
apiVersion: kueue.x-k8s.io/v1beta2
kind: MultiKueueCluster
metadata:
  name: ${WORKER_NAME}
spec:
  clusterSource:
    clusterProfileRef:
      name: ${CLUSTER_PROFILE_NAME}
