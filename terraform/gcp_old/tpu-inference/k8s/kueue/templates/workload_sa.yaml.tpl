# The identity a TPU workload runs as.
#
# Named rather than `default`, which is what every pod in the namespace gets
# when none is set. The cache buckets grant object access to this principal, and
# `default` would hand that to anything scheduled here - including a workload
# image named by a pull request, since allowed_image_repos is still empty and
# the launcher accepts any image it is given.
#
# No GSA annotation: roles are granted directly to
# ${PROJECT}.svc.id.goog[${NAMESPACE}/tpu-workload], so there is nothing to
# impersonate and no token to mint.
apiVersion: v1
kind: ServiceAccount
metadata:
  name: tpu-workload
  namespace: ${NAMESPACE}
