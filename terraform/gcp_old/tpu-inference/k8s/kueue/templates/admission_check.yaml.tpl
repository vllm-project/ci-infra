---
# Makes a ClusterQueue on the manager dispatch rather than run. A workload that
# passes quota is held until MultiKueue has placed it on a worker.
apiVersion: kueue.x-k8s.io/v1beta2
kind: AdmissionCheck
metadata:
  name: ${QUEUE_NAME}-multikueue-dispatch
spec:
  controllerName: kueue.x-k8s.io/multikueue
  parameters:
    apiGroup: kueue.x-k8s.io
    kind: MultiKueueConfig
    name: ${QUEUE_NAME}-workers
