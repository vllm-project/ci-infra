---
# The SSH key an agent pod checks a private repository out with.
#
# A bare-metal agent has one on the VM, in the user's ~/.ssh, put there once
# when the machine was built. A pod has nothing: it is created per job from an
# image, and the checkout container fails with "Permission denied (publickey)"
# on the first clone. Public repositories are unaffected, so this is only what
# vllm-torchtpu needs and tpu-inference does not.
#
# Manager only, like the agent token, and for the same reason: checkout happens
# in the agent pod, which is here. A worker runs the workload container against
# an image that is already built, and never clones.
#
# The key is this fleet's own, beside the agent token, and is registered on the
# repository in GitHub as a read-only deploy key: it can clone that one
# repository and nothing else, which is all a checkout does.
apiVersion: secrets-store.csi.x-k8s.io/v1
kind: SecretProviderClass
metadata:
  name: ${SECRET_NAME}
  namespace: ${NAMESPACE}
spec:
  provider: gke
  parameters:
    secrets: |
      - resourceName: "projects/${PROJECT_ID}/secrets/${GIT_SSH_KEY_SECRET_ID}/versions/latest"
        path: "ssh-key"
---
# The key type is in the target key's name and is not ours to choose: the agent
# reads SSH_PRIVATE_ECDSA_KEY, SSH_PRIVATE_ED25519_KEY or SSH_PRIVATE_RSA_KEY,
# writes the value to a file and starts an ssh-agent holding it. A name that
# does not match the key's algorithm is ignored in silence, and the clone fails
# exactly as it does with no key at all - so this name tracks the algorithm of
# the deploy key in Secret Manager, and rotating to a different one means
# changing it here too.
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
      - sourcePath: ssh-key
        targetKey: ${GIT_SSH_KEY_ENV}
