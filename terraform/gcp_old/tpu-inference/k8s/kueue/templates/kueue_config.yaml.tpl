---
# Overrides the ConfigMap that ships inside the upstream Kueue release, which
# is why the name and namespace are fixed rather than templated: the manager
# Deployment mounts this exact object at /controller_manager_config.yaml.
#
# Applied after the upstream manifests and under our own field manager, so a
# server-side apply of a new Kueue version restores upstream's default and this
# immediately puts ours back. Changing it does not restart anything, so the
# deploy script rolls the Deployment afterwards.
apiVersion: v1
kind: ConfigMap
metadata:
  name: kueue-manager-config
  namespace: kueue-system
data:
  controller_manager_config.yaml: |
${CONFIG}
