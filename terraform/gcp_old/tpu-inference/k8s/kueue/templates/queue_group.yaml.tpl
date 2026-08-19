---
apiVersion: kueue.x-k8s.io/v1beta2
kind: ClusterQueue
metadata:
  name: ${QUEUE_NAME}
spec:
  cohortName: ${ACCELERATOR}
  preemption:
    reclaimWithinCohort: Any
    withinClusterQueue: LowerPriority
  namespaceSelector:
    matchLabels:
      kubernetes.io/metadata.name: ${NAMESPACE}${ADMISSION_CHECKS}
  resourceGroups:
    # google.com/tpu is the only resource under quota. cpu and memory requested
    # by the workload, its gcsfuse sidecar and any helper containers are ignored
    # by admission (quotaCheckStrategy: IgnoreUndeclared in the controller
    # config) and enforced by the kube scheduler against node capacity instead.
    - coveredResources:
        - google.com/tpu
      flavors:
        - name: ${ACCELERATOR}
          resources:
            - name: google.com/tpu
              nominalQuota: ${NOMINAL_QUOTA}
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: LocalQueue
metadata:
  name: ${QUEUE_NAME}
  namespace: ${NAMESPACE}
spec:
  clusterQueue: ${QUEUE_NAME}
