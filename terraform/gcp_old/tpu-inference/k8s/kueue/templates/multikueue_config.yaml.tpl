---
apiVersion: kueue.x-k8s.io/v1beta2
kind: MultiKueueConfig
metadata:
  name: ${ACCELERATOR}-workers
spec:
  clusters:
${WORKER_LIST}
