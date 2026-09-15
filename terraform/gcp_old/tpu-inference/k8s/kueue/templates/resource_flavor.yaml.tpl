---
# One flavor per TPU generation, and deliberately no nodeLabels.
#
# The flavor, not the queue, is Kueue's quota partition, and Kueue skips a
# flavor whose nodeLabels conflict with what a podSet asks for. A flavor per
# shape would therefore stop a 1-chip job borrowing idle 8-chip capacity,
# whatever the numbers said. Placement is the workload's anyway: it carries the
# TPU nodeSelectors itself.
apiVersion: kueue.x-k8s.io/v1beta2
kind: ResourceFlavor
metadata:
  name: ${ACCELERATOR}
