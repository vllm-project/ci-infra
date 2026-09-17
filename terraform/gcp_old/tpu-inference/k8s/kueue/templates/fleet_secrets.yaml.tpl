---
# The credentials a workload pod reads from its own environment, one Secret
# each, from the env_secrets map in prod.auto.tfvars.
#
# Workers only, and the mirror image of the agent token next door. That one is
# an org registration credential and stays on the manager, where the only
# things that register are. These are the opposite: nothing on the manager
# wants them, and the container that does is on a worker.
#
# They arrive as a Secret rather than as a value the launcher resolves and
# writes into the podspec. A value would be plaintext in five objects per job -
# the Job and the Workload on the manager, MultiKueue's copies of both here,
# and the pod - each readable by anything holding get on its kind in this
# namespace. A secretKeyRef is readable by whatever can read this Secret, which
# is the RBAC boundary a credential should have.
#
# What that costs is a credential at rest on a cluster that runs images named
# by a pull request, and that is the bound on what may be listed: a secret the
# fleet lends to every pipeline that asks, in a project this one cannot reach.
# The deploy key and the agent token fail that test and stay on the manager,
# where nothing runs a pull request's image.

# The identity the sync reads Secret Manager as, matching the manager's. Its
# own account rather than tpu-workload: this one may read the fleet's secrets,
# that one may write to the caches, and a workload image named by a pull
# request runs as the second. Nothing mounts this account, so nothing that runs
# here can act as it.
#
# No Google service account annotation: secrets.tf grants the accessor role to
# the Workload Identity principal directly. The pool is the project's, not the
# cluster's, so the same grant covers this account on every cluster in the
# fleet and a new worker needs no IAM of its own.
apiVersion: v1
kind: ServiceAccount
metadata:
  name: secret-sync
  namespace: ${NAMESPACE}
${SECRET_DOCS}
