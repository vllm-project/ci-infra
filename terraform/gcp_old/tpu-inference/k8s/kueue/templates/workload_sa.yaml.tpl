---
# The identity a TPU workload runs as.
#
# Named rather than `default`: the cache buckets grant object access to this
# principal, and `default` is what every pod in the namespace gets when none is
# set - which would extend that access to anything scheduled here.
#
# No Google service account annotation: cache.tf grants the roles directly to
# ${PROJECT_ID}.svc.id.goog[${NAMESPACE}/tpu-workload], so there is nothing to
# impersonate and no key to rotate.
apiVersion: v1
kind: ServiceAccount
metadata:
  name: tpu-workload
  namespace: ${NAMESPACE}
