---
# One flavor per TPU generation, and no nodeLabels that tell its shapes apart.
#
# The flavor, not the queue, is Kueue's quota partition, and Kueue skips a
# flavor whose nodeLabels conflict with what a podSet asks for. A flavor per
# shape would therefore stop a 1-chip job borrowing idle 8-chip capacity,
# whatever the numbers said. Placement is the workload's anyway: it carries the
# TPU nodeSelectors itself.
#
# A generation with Spot names its reservation, which every one of its reserved
# nodes carries and no Spot node does: the key the launcher pins a workload to
# Spot with, since Kueue ignores affinity on keys a flavor does not define
# (reserved_flavor_labels in generate_manifests.py).
apiVersion: kueue.x-k8s.io/v1beta2
kind: ResourceFlavor
metadata:
  name: ${ACCELERATOR}${NODE_LABELS}
