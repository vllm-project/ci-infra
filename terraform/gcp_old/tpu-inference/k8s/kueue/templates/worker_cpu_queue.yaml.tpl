---
# Where a workload that holds no chips is admitted, one queue per worker.
#
# It cannot go through the shape's queue. Kueue decides which AdmissionChecks a
# workload needs by intersecting each check's flavors with the flavors the
# workload was assigned, and a workload is only assigned flavors for resources
# its queue covers. The TPU queues cover google.com/tpu alone, so a pod that
# asks for no chips is assigned no flavor, matches no check, and is admitted on
# the manager - which has nowhere to run it, the Job webhook having already
# marked it as MultiKueue's. It then waits out its deadline having never
# existed anywhere. Covering cpu is what fixes that: it is a resource such a
# pod does request, so the workload gets a flavor and with it the dispatch.
#
# Per worker rather than per shape, because what a prewarm warms is one
# cluster's nodes - the check below names a single worker instead of letting
# Kueue pick. Outside every cohort, because nothing about admitting chip-less
# work should be able to move the TPU queues' accounting.
apiVersion: kueue.x-k8s.io/v1beta2
kind: ClusterQueue
metadata:
  name: ${QUEUE_NAME}
spec:
  preemption:
    reclaimWithinCohort: Never
    withinClusterQueue: LowerPriority
  namespaceSelector:
    matchLabels:
      kubernetes.io/metadata.name: ${NAMESPACE}${ADMISSION_CHECKS}
  resourceGroups:
    - coveredResources:
        - cpu
      flavors:
        - name: ${FLAVOR}
          resources:
            - name: cpu
              # A ceiling on chip-less work running at once, not a reservation:
              # the compute class creates these nodes on demand and scales them
              # back to none. High enough that a prewarm never queues behind
              # another build's, low enough that a loop submitting them cannot
              # fill the region.
              nominalQuota: ${NOMINAL_QUOTA}
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: LocalQueue
metadata:
  name: ${QUEUE_NAME}
  namespace: ${NAMESPACE}
spec:
  clusterQueue: ${QUEUE_NAME}
