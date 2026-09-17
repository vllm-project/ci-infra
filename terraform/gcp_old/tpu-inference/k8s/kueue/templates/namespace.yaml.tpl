---
apiVersion: v1
kind: Namespace
metadata:
  name: ${NAMESPACE}
  labels:
    # baseline enforced rather than restricted: a TPU pod needs the
    # google.com/tpu device, and the gcsfuse sidecar GKE injects runs with
    # settings restricted rejects. warn/audit stay at restricted so anything
    # that does not need the exemption still shows up.
    pod-security.kubernetes.io/enforce: baseline
    pod-security.kubernetes.io/audit: restricted
    pod-security.kubernetes.io/warn: restricted${EXTRA_LABELS}
