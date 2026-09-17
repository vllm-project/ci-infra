---
# What a node on the manager is allowed to be. The cluster declares no node
# pools at all: GKE creates one for a pod that is already pending, and this is
# the order it tries machine families in before giving up on the list.
#
# The fallback is the whole point. us-central1 runs out of e2 often enough that
# a control plane pinned to one family is a stockout away from the fleet having
# no agents, and none of the pods here - the Kueue and JobSet controllers, the
# Buildkite controller, one launcher pod per TPU step in flight - care what they
# run on. They want cores to drive kubectl and a Python script, and nothing
# else, so any family that can offer four of them will do.
#
# n2 first because it is the widest-stocked general family in this region, n2d
# and c3 next as different supply pools rather than different performance, and
# e2 last because it is the one that ran out.
apiVersion: cloud.google.com/v1
kind: ComputeClass
metadata:
  name: ${NAME}
spec:
  priorities:
  - machineFamily: n2
    minCores: 4
  - machineFamily: n2d
    minCores: 4
  - machineFamily: c3
    minCores: 4
  - machineFamily: e2
    minCores: 4

  # Exhausting the list falls back to letting GKE pick anything, rather than
  # leaving the pod pending. A control-plane pod on an odd machine type is a
  # cost surprise; a control-plane pod that never schedules stops the fleet.
  whenUnsatisfiable: ScaleUpAnyway

  nodePoolAutoCreation:
    enabled: true
