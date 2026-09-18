---
# The workers that have a node pool of this shape, and so the ones an
# AdmissionCheck for it may place onto. Kueue picks the first that admits the
# workload.
apiVersion: kueue.x-k8s.io/v1beta2
kind: MultiKueueConfig
metadata:
  name: ${QUEUE_NAME}-workers
spec:
  clusters:
${WORKER_LIST}
