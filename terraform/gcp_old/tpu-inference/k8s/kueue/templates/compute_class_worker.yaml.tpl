---
# Where a workload role that holds no chips runs. A benchmark client driving the
# engines over HTTP is part of the test and lands on this cluster, but it wants
# cores rather than a TPU, and putting it on a TPU node spends four chips on a
# Python process.
#
# A node created for the pod is sized by the pod, so the cores exist while a
# benchmark is running and not otherwise, and a role that wants more of them is
# a number in a manifest rather than a node pool replacement.
#
# No namespace names this as its default. The workload namespace is shared with
# the TPU roles, and a default class would add its node selector to those too,
# on top of the TPU selectors they already carry - a combination no node
# satisfies. A role that holds no chips selects the class itself.
apiVersion: cloud.google.com/v1
kind: ComputeClass
metadata:
  name: ${NAME}
spec:
  # Ordered by supply rather than by speed: the work is HTTP request generation,
  # which none of these families does distinguishably better, so the list is
  # there to make one family being out of stock a slower scale-up instead of a
  # failed test. e2 last, because it is the one this region runs out of.
  #
  # minCores is a floor, not a size - GKE fits the node to the pending pod's
  # requests - and only stops it picking something too small to be worth a node.
  priorities:
  - machineFamily: n2
    minCores: 8
  - machineFamily: n2d
    minCores: 8
  - machineFamily: c3
    minCores: 8
  - machineFamily: e2
    minCores: 8

  # Leave the pod pending rather than take a node from outside the list. The
  # fallback GKE offers is the cluster's default node configuration, which means
  # the default machine series - e2, the one this ordering exists to avoid - at
  # a size that ignores minCores. A benchmark that runs on a node a quarter the
  # size it asked for still reports numbers, and they are wrong; a pending pod
  # says so in a scheduler event. For a lane whose output is measurements, no
  # answer beats a plausible wrong one.
  whenUnsatisfiable: DoNotScaleUp

  nodePoolConfig:
    # The same restricted node identity every other node on this cluster runs
    # as. Without it these nodes fall through to the Compute Engine default
    # account, which on this project carries tpu.admin, storage.admin and
    # project-wide secretmanager.secretAccessor because the bare-metal agent VMs
    # share it. Workload Identity keeps a pod from minting a token for it, so
    # what this closes is the kubelet's own reach, not the pods'.
    serviceAccount: ${NODE_SERVICE_ACCOUNT}

    # These nodes are created for a pending pod, so every image they run is a
    # cold pull of a CI image built per commit. Streaming lets the container
    # start on the part of the image it has, which is most of the cold start.
    imageStreaming:
      enabled: true

  nodePoolAutoCreation:
    enabled: true
