---
# A generation's Spot capacity, beside its reserved flavor, for the shapes that
# have a Spot pool (tpu_node_pools[*].spot). A queue lists it after the reserved
# flavor, so Kueue admits onto Spot only when the reservation cannot take the
# workload; see FLAVOR_FUNGIBILITY in generate_manifests.py.
#
# Unlike the reserved flavor this one does name nodes: Kueue writes nodeLabels
# into the pod's nodeSelector and adds the tolerations when it admits a
# workload on this flavor, and only then. cloud.google.com/gke-spot is GKE's own
# label on every Spot node; the toleration answers the taint workers.tf puts on
# the Spot pools, which keeps every pod admitted on the reserved flavor off
# them. So the reserved flavor, its pools and the pods already running need no
# change for Spot to exist beside them.
apiVersion: kueue.x-k8s.io/v1beta2
kind: ResourceFlavor
metadata:
  name: ${ACCELERATOR}-spot
spec:
  nodeLabels:
    cloud.google.com/gke-spot: "true"
  tolerations:
    - key: cloud.google.com/gke-spot
      operator: Equal
      value: "true"
      effect: NoSchedule
