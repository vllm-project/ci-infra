---
apiVersion: kueue.x-k8s.io/v1beta2
kind: MultiKueueCluster
metadata:
  name: ${WORKER_NAME}
spec:
  clusterSource:
    clusterProfileRef:
      name: ${CLUSTER_PROFILE_NAME}
