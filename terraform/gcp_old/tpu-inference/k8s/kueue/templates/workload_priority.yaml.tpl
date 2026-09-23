---
# What a workload's place in the queue is worth, mirroring the rungs
# bootstrap.sh applies on bare metal. A workload naming no class scores 0.
#
# Ordering only - nothing running is ever taken away; see queue_group.yaml.tpl.
apiVersion: kueue.x-k8s.io/v1beta2
kind: WorkloadPriorityClass
metadata:
  name: ${PRIORITY_NAME}
value: ${PRIORITY_VALUE}
description: "${PRIORITY_DESCRIPTION}"
