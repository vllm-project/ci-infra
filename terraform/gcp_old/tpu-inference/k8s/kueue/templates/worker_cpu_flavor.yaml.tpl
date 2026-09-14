---
# The chip-less half of a worker: the nodes the ${NAME} ComputeClass creates,
# which hold no accelerator and carry no TPU taint.
#
# nodeLabels here, where the TPU flavors deliberately have none. A TPU workload
# states its own placement and its flavor is only a quota partition; this one is
# the whole statement of where a chip-less workload goes, because Kueue adds a
# flavor's nodeLabels to the pods it admits. A role that names the class itself
# gets the same answer twice, and one that forgets still cannot land on chips.
apiVersion: kueue.x-k8s.io/v1beta2
kind: ResourceFlavor
metadata:
  name: ${NAME}
spec:
  nodeLabels:
    cloud.google.com/compute-class: ${NAME}
