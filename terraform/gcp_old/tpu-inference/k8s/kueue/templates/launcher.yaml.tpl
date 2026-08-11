---
# TPU workload launcher.
#
# Every TPU step runs here: the launcher submits the real workload (a Job for
# single-pod work, a JobSet for anything multi-pod) and owns its lifecycle.
# The launcher pod is CPU-only and carries no Kueue queue label, so Kueue
# manages only the submitted workload - which is what keeps the Buildkite agent
# alive across preemption and out of the reservation window.
#
# Profiles and templates are generated from the same tfvars as the Kueue
# objects, so placement cannot be invented by a pipeline and cannot drift from
# the queues it targets.
apiVersion: v1
kind: ServiceAccount
metadata:
  name: tpu-launcher
  namespace: ${NAMESPACE}
  annotations:
    iam.gke.io/gcp-service-account: ${LAUNCHER_GSA}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: tpu-launcher
  namespace: ${NAMESPACE}
rules:
  # The workloads the launcher submits and owns.
  - apiGroups: ["batch"]
    resources: ["jobs"]
    verbs: ["create", "get", "list", "watch", "delete"]
  - apiGroups: ["jobset.x-k8s.io"]
    resources: ["jobsets"]
    verbs: ["create", "get", "list", "watch", "delete"]
  # Read-only: admission state, so a step waiting on a node pool scale-up says
  # so instead of sitting silent.
  - apiGroups: ["kueue.x-k8s.io"]
    resources: ["workloads"]
    verbs: ["get", "list", "watch"]
  # Its own pod, to find the owning Job for the workload's ownerReference.
  - apiGroups: [""]
    resources: ["pods"]
    verbs: ["get"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: tpu-launcher
  namespace: ${NAMESPACE}
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: tpu-launcher
subjects:
  - kind: ServiceAccount
    name: tpu-launcher
    namespace: ${NAMESPACE}
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: tpu-launcher-scripts
  namespace: ${NAMESPACE}
data:
  launch: |
${LAUNCHER_SCRIPT}
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: tpu-launcher-profiles
  namespace: ${NAMESPACE}
data:
  profiles.yaml: |
${LAUNCHER_PROFILES}
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: tpu-launcher-templates
  namespace: ${NAMESPACE}
data:
${LAUNCHER_TEMPLATES}
---
# Referenced from a pipeline step as:
#   plugins:
#     - kubernetes: { podTemplate: tpu-launcher }
#   command: /opt/launcher/launch --profile v6e-8-2x4 -- pytest tests/
apiVersion: v1
kind: PodTemplate
metadata:
  name: tpu-launcher
  namespace: ${NAMESPACE}
template:
  spec:
    serviceAccountName: tpu-launcher
    containers:
      # Needs kubectl (submit and watch the workload) and gcloud (read worker
      # cluster pod logs from Cloud Logging - the pods run in a different
      # cluster, so kubectl logs here returns nothing).
      - name: launcher
        image: ${LAUNCHER_IMAGE}
        env:
          - name: LAUNCHER_NAMESPACE
            value: ${NAMESPACE}
          - name: LAUNCHER_POD_NAME
            valueFrom:
              fieldRef:
                fieldPath: metadata.name
        resources:
          requests:
            cpu: "200m"
            memory: 512Mi
          limits:
            cpu: "1"
            memory: 1Gi
        volumeMounts:
          - name: launcher-scripts
            mountPath: /opt/launcher
          - name: launcher-profiles
            mountPath: /opt/launcher/profiles
          - name: launcher-templates
            mountPath: /opt/launcher/templates
    volumes:
      - name: launcher-scripts
        configMap:
          name: tpu-launcher-scripts
          defaultMode: 0755
      - name: launcher-profiles
        configMap:
          name: tpu-launcher-profiles
      - name: launcher-templates
        configMap:
          name: tpu-launcher-templates
