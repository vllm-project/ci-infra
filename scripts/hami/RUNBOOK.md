# GPU Sharing Node (HAMi) Runbook

How to turn a Kubernetes GPU node into a **sharing node**: several CI Pods run
on one GPU, each held to a GPU-memory quota by [HAMi](https://github.com/Project-HAMi/HAMi).
Follow the steps in order. Each ends on a **Done when** line; check it before
moving on.

Use this for a node in a cluster that runs the NVIDIA GPU Operator and serves
Buildkite through agent-stack-k8s (the B200 and H100 clusters). For a
Docker-based machine, see [`../AGENTS.md`](../AGENTS.md) instead; for slicing
H200s with MIG, see [`../buildkite-agent/MIG_RUNBOOK.md`](../buildkite-agent/MIG_RUNBOOK.md).

## The big picture

```
CI Pod with label vllm.ci/gpu-sharing=hami and limit nvidia.com/gpumem
   └─ HAMi webhook (sees only labelled Pods)   → routes the Pod to hami-scheduler
   └─ hami-scheduler (2 replicas, binpack)     → picks a card with enough free quota
   └─ sharing node: label gpu=on, taint nvidia.com/gpu=hami:NoSchedule
        └─ HAMi device plugin (replaces the GPU Operator's on this node)
             advertises each GPU 10 times and enforces the quota in the container
Every other Pod                                 → default scheduler, whole cards, never this node
```

- A labelled Pod **with** `nvidia.com/gpumem` gets a share of a card.
- A labelled Pod **without** it gets whole, exclusive cards on the sharing node.
- The taint keeps ordinary CI Pods off the node. The GPU Operator and HAMi
  daemonsets already tolerate the `nvidia.com/gpu` key, so they keep running.

## Prerequisites

- `kubectl` with cluster-admin on the cluster's control plane. Commands below
  run there.
- Helm 3 or 4 on the control plane. HAMi's certificate Jobs are Helm hooks, so
  install with `helm install`; `helm template | kubectl apply` loses their order.
- The control plane can pull `docker.io/projecthami/hami`,
  `registry.k8s.io/kube-scheduler` and
  `registry.k8s.io/ingress-nginx/kube-webhook-certgen`.
- Approval to take the node out of normal CI. From step 1 until step 5 its GPUs
  serve no CI jobs.

## Step 1: take the node out of CI

```bash
NODE=<node>
kubectl cordon "$NODE"
# Wait until no CI Pod is left on the node.
kubectl get pods -n buildkite --field-selector spec.nodeName="$NODE"
```

Running jobs finish on their own; cordoning only stops new ones.

**Done when** the node shows `SchedulingDisabled` and the `buildkite` namespace
has no Pods on it.

## Step 2: check the toolkit, then install HAMi

1. On every incoming node, compare the host's container toolkit with the GPU
   Operator's:
   ```bash
   /usr/bin/nvidia-ctk --version
   /usr/local/nvidia/toolkit/nvidia-ctk --version
   ```
   If the host's is older, HAMi needs `nvidiaHookPath` (uncomment it in
   [`values.yaml`](values.yaml)): an older host `nvidia-ctk` fails container
   start on hook names it does not know (see Troubleshooting). The setting
   applies to every sharing node in the cluster; the operator's hook exists on
   all GPU Operator nodes, so it is safe on the others too.
2. If `kubectl get pods -n hami-system` already lists `hami-scheduler` Pods,
   HAMi is installed: skip the install below. When check 1 needs
   `nvidiaHookPath` and the installed release lacks it
   (`helm get values hami -n hami-system`), add it with
   `helm upgrade hami hami --repo https://project-hami.github.io/HAMi --version 2.11.0 -n hami-system -f values.yaml --wait`.
   The upgrade restarts HAMi's device plugin on every sharing node, so run it
   while no shared job is running. Then go to step 3.
3. First sharing node in the cluster: copy `values.yaml` to the control plane
   and install:
   ```bash
   helm install hami hami --repo https://project-hami.github.io/HAMi \
     --version 2.11.0 -n hami-system --create-namespace \
     -f values.yaml --wait --timeout 10m
   ```
4. Check it:
   ```bash
   kubectl get pods -n hami-system -o wide   # 2 hami-scheduler Pods Running
   kubectl get mutatingwebhookconfiguration | grep hami
   ```

The install alone changes nothing for CI: the webhook only sees labelled Pods,
and the device plugin runs only on nodes labelled `gpu=on`.

**Done when** the toolkit check is recorded for this node, `helm status hami -n hami-system` says `deployed` with `nvidiaHookPath` set if the check needed it, both
`hami-scheduler` Pods are Running, and the webhook exists.

## Step 3: hand the node to HAMi

```bash
kubectl label node "$NODE" nvidia.com/gpu.deploy.device-plugin=false gpu=on --overwrite
```

The first label removes the GPU Operator's device plugin from this node only;
the second starts HAMi's. The swap takes about 30 seconds.

```bash
kubectl get node "$NODE" -o jsonpath='{.status.allocatable.nvidia\.com/gpu}{"\n"}'
kubectl get pods -n hami-system -o wide | grep "$NODE"          # hami-device-plugin Running
kubectl get pods -n gpu-operator -o wide | grep "$NODE" | grep device-plugin   # nothing
```

**Done when** allocatable `nvidia.com/gpu` is 10 times the GPU count (80 on an
8-GPU node), `hami-device-plugin` is Running on the node, and the operator's
device-plugin Pod is gone from it.

## Step 4: smoke test

Run one quota Pod on the node. It tolerates the cordon, so the node stays out
of CI meanwhile.

```bash
kubectl create ns gpu-sharing-test
kubectl apply -f - <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: smoke-quota
  namespace: gpu-sharing-test
  labels: {vllm.ci/gpu-sharing: hami}
spec:
  restartPolicy: Never
  runtimeClassName: nvidia
  nodeSelector: {kubernetes.io/hostname: $NODE}
  tolerations:
    - {key: node.kubernetes.io/unschedulable, operator: Exists, effect: NoSchedule}
  containers:
    - name: test
      image: docker.io/nvidia/cuda:12.8.1-base-ubuntu24.04
      command: ["nvidia-smi", "--query-gpu=uuid,memory.total", "--format=csv,noheader"]
      resources:
        limits: {nvidia.com/gpu: 1, nvidia.com/gpumem: 40000}
EOF
kubectl logs -n gpu-sharing-test smoke-quota   # once the Pod is Completed
kubectl delete ns gpu-sharing-test
```

**Done when** the log shows `GPU-<uuid>, 40000 MiB` followed by `HAMI-core`
cleanup lines: the container saw the quota, not the whole card.

## Step 5: taint, then uncordon

Taint first. Uncordoning an untainted sharing node lets ordinary CI Pods land
there, and HAMi's device plugin refuses them (`UnexpectedAdmissionError`).

Before tainting, list the DaemonSets that would not tolerate the taint:

```bash
kubectl get ds -A -o json | jq -r '.items[]
  | select([.spec.template.spec.tolerations[]?
      | select(((.effect // "") == "" or .effect == "NoSchedule")
        and ((.operator == "Exists" and ((.key // "") == "" or .key == "nvidia.com/gpu"))
          or ((.operator // "Equal") == "Equal"
              and .key == "nvidia.com/gpu" and .value == "hami")))] | length == 0)
  | "\(.metadata.namespace)/\(.metadata.name)"'
```

Their running Pods stay, but a Pod recreated later (a DaemonSet update, a
reboot) cannot return to the node. Add the toleration to each one the node
needs, such as the containerd image cleanup that keeps the root disk from
filling:

```bash
kubectl get ds -n <namespace> <name> -o json \
  | jq '.spec.template.spec.tolerations = ((.spec.template.spec.tolerations // [])
        + [{"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}])' \
  | kubectl replace -f -
```

The patch rolls that DaemonSet's Pods on every node. Then:

```bash
kubectl taint node "$NODE" nvidia.com/gpu=hami:NoSchedule
kubectl uncordon "$NODE"
```

**Done when** the list above holds only DaemonSets this node does not need,
`kubectl describe node "$NODE"` lists the taint, and it no longer says
`Unschedulable: true`.

## Step 6: send steps to the node

A step runs on the sharing node when its Pod spec has all four:

| Pod field | Value |
|---|---|
| `metadata.labels` | `vllm.ci/gpu-sharing: hami` |
| container `resources.limits` | `nvidia.com/gpu: 1` and `nvidia.com/gpumem: <MiB per GPU>` |
| `nodeSelector` | `gpu: "on"` |
| `tolerations` | `{key: nvidia.com/gpu, operator: Equal, value: hami, effect: NoSchedule}` |

The pipeline generator builds these Pod specs in `get_k8s_plugin`
([`plugin/k8s_plugin.py`](../../buildkite/pipeline_generator/plugin/k8s_plugin.py)).
agent-stack-k8s copies the plugin's `metadata.labels` onto the Pod. A generator
change runs in every build: render the pipeline before and after, and run a
scoped staging build before it merges.

Find each step's quota by measurement, never from the whole-card peak alone:

1. Run the step on a whole card through HAMi (labelled, no `gpumem`) and at a
   candidate quota, both as Pods on the sharing node, with `pytest -rA` so
   every test's outcome is in the log.
2. Compare the two runs test by test (node IDs and outcomes). Counts alone can
   hide a skipped test.
3. Take the smallest quota whose run matches. Peak + 10% is a first guess
   that often fails; steps that start a vLLM engine need more (see below).

Rules learned from the first trials:

- A vLLM engine wants `gpu_memory_utilization` (0.92 by default) of the memory
  it sees free when it starts. Memory still held by earlier tests in the same
  pytest process counts against that, so a step that starts engines needs a
  quota of at least 12.5 times the held memory (held / 0.08), however small
  its peak. A whole card hides this. The failure names both numbers:
  `Free memory on device cuda:0 (<free>/<quota> GiB) on startup is less than
  desired GPU memory utilization`; held is quota minus free. Shards change
  which tests share a process, so check every shard.
- Tests that size themselves from the card skip or shrink under a quota:
  `large_gpu_mark(min_gb=N)` reads the quota as the card size and can skip
  while the step stays green; a fixed `gpu_memory_utilization` fraction of a
  small quota can leave no KV cache.
- Multi-GPU steps need headroom for NCCL setup above their traced peak; keep
  them on whole cards until measured.
- A quota that is too small usually fails loudly: a torch OOM, or a HAMi-core
  line `OOM <used> / <limit>`. The used figure is a better quota guess than the
  traced peak.

**Done when** each listed step has a measured quota and a staging build
compared test by test against `main` shows no lost or newly skipped test.

## Rollback

```bash
kubectl cordon "$NODE"
# wait for the shared Pods to finish, then:
kubectl taint node "$NODE" nvidia.com/gpu=hami:NoSchedule-
kubectl label node "$NODE" gpu- nvidia.com/gpu.deploy.device-plugin=true --overwrite
# the GPU Operator's device plugin returns on its own; check allocatable is the GPU count again
kubectl uncordon "$NODE"
# last sharing node in the cluster only:
helm uninstall hami -n hami-system && kubectl delete ns hami-system
```

Remove the step opt-ins from the generator first, or their Pods wait Pending
for a node that no longer exists. HAMi leaves `/usr/local/vgpu/` and
`/var/run/hami` on the node's disk; both are inert.

## What changes for monitoring

Readings taken by exec into the GPU Operator's device-plugin Pod (driver, Xid
and NVLink fabric checks) have nothing to exec into on a sharing node, so they
go missing for it. A check that starts an unlabelled Pod on the node to verify
it is refused there. Account for both before trusting quarantine or GPU alerts
for that node.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `failed to inject CDI devices: unresolvable CDI devices management.nvidia.com/gpu=GPU-<uuid>` | `deviceListStrategy` left at `envvar` with the operator's CDI-mode runtime | `deviceListStrategy: cdi-annotations` (in `values.yaml`) |
| Pod packed but never starts: `createRuntime hook ... No help topic for 'apply-cuda-memory-limits'` | host `/usr/bin/nvidia-ctk` older than the operator's | set `nvidiaHookPath`, `helm upgrade` (step 2) |
| Ordinary CI Pod on the node fails `UnexpectedAdmissionError ... no node lock` | node uncordoned without the taint | step 5 |
| Quota Pod stuck Pending `Insufficient nvidia.com/gpumem` after a scheduler outage | it was admitted while `hami-scheduler` was down | delete and recreate it; keep 2 replicas and `failurePolicy: Fail` |
| Test Pod `ErrImageNeverPull` with `imagePullPolicy: Never` | the cached image was pulled with a pull secret | use `IfNotPresent` |
| Step green under a quota but with fewer tests run | a test reads the quota as the card size | raise the quota or keep the step whole |
| `Free memory on device cuda:0 (<free>/<quota> GiB) on startup is less than desired GPU memory utilization` | earlier tests in the process still hold memory | quota at least (quota - free) / 0.08 (step 6) |
| `No available memory for the cache blocks` | the test sizes its engine as a fraction of the visible memory | keep the step near a whole card |
| `NCCL split failed` with a HAMi-core OOM line | quota too tight for NCCL setup | raise the quota or keep the step whole |
| A test fails only on a memory-release check reading near the whole card (`Current: <N> GB`) | HAMi reports NVML memory wrongly after release | keep that step on whole cards |

## Quick checklist

- [ ] Node cordoned and empty of CI Pods (step 1)
- [ ] Toolkit checked on this node; HAMi installed or upgraded with `values.yaml`, 2 scheduler replicas (step 2)
- [ ] Node labelled `gpu=on` and `nvidia.com/gpu.deploy.device-plugin=false`, advertising 10 per GPU (step 3)
- [ ] Smoke Pod saw its 40000 MiB quota (step 4)
- [ ] Needed DaemonSets tolerate the taint; taint `nvidia.com/gpu=hami:NoSchedule` on, then uncordoned (step 5)
- [ ] Steps opted in with measured quotas, staging build compared with `main` (step 6)
