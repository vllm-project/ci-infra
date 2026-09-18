---
# One queue per TPU shape, all of a generation's queues in one cohort, so that
# nominalQuota is chips this shape can always have and no other shape can hold
# the reservation against it, while whatever it is not using is lent out.
#
# reclaimWithinCohort stays Never: a lender waits for the borrower's workloads
# to finish rather than evicting them. Eviction returns the accounting at once
# but not the hardware, since eight chips freed across eight single-chip nodes
# still have to scale down before an 8-chip node can boot, and the jobs killed
# to get there have to run again.
apiVersion: kueue.x-k8s.io/v1beta2
kind: ClusterQueue
metadata:
  name: ${QUEUE_NAME}
spec:
  cohortName: ${ACCELERATOR}
  preemption:
    reclaimWithinCohort: Never
    withinClusterQueue: LowerPriority
  namespaceSelector:
    matchLabels:
      kubernetes.io/metadata.name: ${NAMESPACE}${ADMISSION_CHECKS}
  resourceGroups:
    # google.com/tpu is the only resource under quota; everything else a pod
    # requests is left to the kube scheduler. See quotaCheckStrategy in
    # common-config.yaml.
    - coveredResources:
        - google.com/tpu
      flavors:
        - name: ${ACCELERATOR}
          resources:
            - name: google.com/tpu
              # Chips, not nodes. This is a ceiling on what Kueue will admit at
              # once, not a promise that a slice of the right shape is free.
              nominalQuota: ${NOMINAL_QUOTA}
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: LocalQueue
metadata:
  name: ${QUEUE_NAME}
  namespace: ${NAMESPACE}
spec:
  clusterQueue: ${QUEUE_NAME}
