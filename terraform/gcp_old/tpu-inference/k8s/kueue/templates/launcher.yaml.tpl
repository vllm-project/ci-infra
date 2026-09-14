---
# The TPU workload launcher.
#
# Every TPU step runs here: the launcher submits the real workload - a Job for
# single-pod work, a JobSet for anything spanning hosts - and owns its
# lifecycle. The launcher pod is CPU-only and carries no Kueue queue label, so
# Kueue manages only the submitted workload; that is what keeps the Buildkite
# agent alive across preemption and out of the reservation window.
#
# Manager only. MultiKueue is what puts the workload on a worker, and all a
# worker needs from the launcher is log read - launcher_rbac_worker.yaml.tpl.
#
# The program it runs is not here: deploy_manifests.py builds the
# tpu-launcher-scripts ConfigMap from kueue/launcher/launch.py at deploy time,
# so the script exists once, as a file that can be linted and run.
apiVersion: v1
kind: ServiceAccount
metadata:
  name: tpu-launcher
  namespace: ${NAMESPACE}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: tpu-launcher
  namespace: ${NAMESPACE}
rules:
  # The workloads the launcher submits and owns. delete is for cancellation:
  # on SIGTERM the launcher removes the workload rather than leaving chips
  # running with nobody watching.
  - apiGroups: ["batch"]
    resources: ["jobs"]
    verbs: ["create", "get", "list", "watch", "delete"]
  - apiGroups: ["jobset.x-k8s.io"]
    resources: ["jobsets"]
    verbs: ["create", "get", "list", "watch", "delete"]
  # Read-only: admission state, so a step waiting on quota or on a node pool
  # scale-up says so instead of sitting silent.
  - apiGroups: ["kueue.x-k8s.io"]
    resources: ["workloads"]
    verbs: ["get", "list", "watch"]
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
# The hardware the fleet has, and what follows from each shape of it.
#
# Generated from the same tfvars as the node pools and the queues, so a profile
# cannot describe hardware the fleet does not have. This is what the launcher
# resolves a workload against: which queue admits it, how long it may hold the
# chips, how large its file cache may be - none of which a manifest states.
apiVersion: v1
kind: ConfigMap
metadata:
  name: tpu-launcher-profiles
  namespace: ${NAMESPACE}
data:
  profiles.yaml: |
${LAUNCHER_PROFILES}
---
# Referenced from a pipeline step as:
#
#   agents: { queue: kube }
#   plugins:
#     - kubernetes: { podTemplate: tpu-launcher }
#   command: /opt/launcher/launch --machine-type ct6e-standard-8t
#            --topology 2x4 -- pytest tests/
apiVersion: v1
kind: PodTemplate
metadata:
  name: tpu-launcher
  namespace: ${NAMESPACE}
template:
  spec:
    serviceAccountName: tpu-launcher
    # Pod-level: agent-stack-k8s adds the agent and the checkout to this pod and
    # they share the workspace, so a UID set on the one container below would
    # leave the checkout root-owned and unwritable by it.
    #
    # fsGroup is what makes that work - the kubelet hands an emptyDir to the
    # fsGroup group-writable, whatever UID each container runs as.
    securityContext:
      runAsNonRoot: true
      runAsUser: 1000
      runAsGroup: 1000
      fsGroup: 1000
      seccompProfile:
        type: RuntimeDefault
    containers:
      # Needs kubectl (submit and watch the workload, read pod logs on the
      # worker), gcloud (Connect Gateway credentials for it), and
      # gke-gcloud-auth-plugin, without which those credentials are useless.
      - name: launcher
        image: ${LAUNCHER_IMAGE}
        securityContext:
          allowPrivilegeEscalation: false
          capabilities:
            drop: ["ALL"]
        env:
          # The image defaults both to /root, which UID 1000 cannot write, and
          # neither is optional: gcloud writes a config directory on its first
          # invocation and kubectl a discovery cache under HOME. Unset, the
          # launcher's first `gcloud` call fails on a mkdir. Not a volume -
          # nothing here outlives the pod.
          - name: HOME
            value: /tmp
          - name: CLOUDSDK_CONFIG
            value: /tmp/gcloud
          - name: LAUNCHER_NAMESPACE
            value: ${NAMESPACE}
          # The pod owns the workload it submits, so GC removes it the moment
          # the pod goes - including the paths that deliver no SIGTERM. An
          # ownerReference needs both, and a pod cannot read its own UID.
          - name: LAUNCHER_POD_NAME
            valueFrom:
              fieldRef:
                fieldPath: metadata.name
          - name: LAUNCHER_POD_UID
            valueFrom:
              fieldRef:
                fieldPath: metadata.uid
        # Memory is what the launcher actually uses - kubectl and gcloud are
        # both Python, and a poll's worth of log lines is held in memory - so
        # that is what it reserves.
        #
        # CPU is a packing number here rather than a need. A queued step is a
        # launcher pod polling on an interval, and the queue is allowed to be a
        # day deep, so the manager is sized by how many steps are waiting rather
        # than by how many are running. The request is what decides how many of
        # those fit on a node; with no cpu limit, a launcher that briefly needs
        # more than it reserved still gets it.
        #
        # A memory limit and no cpu limit. The launcher shares its nodes with
        # the controllers that run the fleet, so a step whose workload floods
        # the log must not be able to take the Kueue controller down with it;
        # cpu is left unbounded because throttling a poller only makes it slower
        # to notice its workload finished.
        resources:
          requests:
            cpu: "100m"
            memory: 1Gi
          limits:
            memory: 2Gi
        volumeMounts:
          - name: launcher-scripts
            mountPath: /opt/launcher
          - name: launcher-profiles
            mountPath: /opt/launcher/profiles
          - name: launcher-manifests
            mountPath: /opt/launcher/manifests
    volumes:
      - name: launcher-scripts
        configMap:
          name: tpu-launcher-scripts
          # Executable: the step's command is the path itself.
          defaultMode: 0755
      - name: launcher-profiles
        configMap:
          name: tpu-launcher-profiles
      # The Job the launcher supplies itself, for a step that names hardware
      # and nothing else.
      - name: launcher-manifests
        configMap:
          name: tpu-launcher-manifests
