---
# The Buildkite agent token, pulled out of Secret Manager and into a Kubernetes
# Secret the agent-stack-k8s chart can be pointed at.
#
# GKE's own SecretSync, not External Secrets: the reconciler runs in the control
# plane, so there is no controller to install and nothing for the deploy script
# to wait on. Enabling it is `secret_sync_config` in manager.tf.
#
# Manager only. This is the org registration token, and the only things that
# register are the controller and the agent pods it creates, both here. A TPU
# pod does call `buildkite-agent` - it is the only thing that can see its own
# output files - but with BUILDKITE_AGENT_ACCESS_TOKEN, which is scoped to the
# one job and forwarded as a plain environment variable. Syncing this one to a
# worker would put a long-lived org credential next to pods running images
# named by a pull request, and buy nothing.

# The identity the sync reads Secret Manager as. Its own, not tpu-workload:
# one may read the agent token, the other may write to the caches, and a
# workload image named by a pull request runs as the second. Nothing mounts
# this account, so nothing that runs here can use it.
#
# No Google service account annotation: secrets.tf grants the accessor role
# straight to the Workload Identity principal, so there is nothing to
# impersonate and no key to rotate.
apiVersion: v1
kind: ServiceAccount
metadata:
  name: secret-sync
  namespace: ${NAMESPACE}
---
# Which secret, and what to call it on the way through. `path` is internal - it
# names the value for the SecretSync below, and is never a file anywhere, since
# nothing mounts this class as a CSI volume. `versions/latest` because rotation
# is the point: a new version is picked up on the next poll.
apiVersion: secrets-store.csi.x-k8s.io/v1
kind: SecretProviderClass
metadata:
  name: ${SECRET_NAME}
  namespace: ${NAMESPACE}
spec:
  provider: gke
  parameters:
    secrets: |
      - resourceName: "projects/${PROJECT_ID}/secrets/${AGENT_TOKEN_SECRET_ID}/versions/latest"
        path: "agent-token"
---
# The Secret itself, which takes this object's own name. The key is not ours to
# choose - agent-stack-k8s reads BUILDKITE_AGENT_TOKEN out of whatever Secret it
# is pointed at - but the name is, and it comes from the generator, because the
# chart's values have to name the same Secret.
#
# Rotation updates this object in place, within about five minutes of a new
# Secret Manager version: there is no watch, only a poll. The controller holds
# its copy for the life of its process, so rotating the token also means
# restarting that Deployment.
apiVersion: secret-sync.gke.io/v1
kind: SecretSync
metadata:
  name: ${SECRET_NAME}
  namespace: ${NAMESPACE}
spec:
  serviceAccountName: secret-sync
  secretProviderClassName: ${SECRET_NAME}
  secretObject:
    type: Opaque
    data:
      - sourcePath: agent-token
        targetKey: BUILDKITE_AGENT_TOKEN
