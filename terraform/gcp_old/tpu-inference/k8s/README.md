# MultiKueue TPU CI/CD Infrastructure on GKE

This directory contains the Terraform configuration, Kueue manifest templates, and manifest generator script for managing the MultiKueue-based TPU CI/CD testing infrastructure across GKE clusters.

---

## 1. Overview & Architecture

The infrastructure uses a **MultiKueue** architecture to distribute TPU CI benchmark workloads submitted via Buildkite across dedicated GKE clusters in Google Cloud Platform:

```
                  +----------------------------------------------+
                  |              Manager Cluster                 |
                  |     Project: cloud-ullm-inference-ci-cd      |
                  |           Name: tpu-ci-manager               |
                  |          Region: us-central1                 |
                  +----------------------+-----------------------+
                                         |
                                Buildkite Agent Stack
                                    (Controller)
                                         |
                                 Kueue MultiKueue
                               (Admission & Dispatch)
                                         |
                                         v
                     +----------------------------------------+
                     |             Worker Cluster             |
                     |      Project: cloud-tpu-inference-test |
                     | Name: tpu-ci-southamerica-west1-a      |
                     |     Location: southamerica-west1-a     |
                     +-------------------+--------------------+
                                         |
                                 GKE TPU Node Pools
                             (v6e-1: 1-chip, v6e-8: 8-chip)
```

---

## 2. Key Accomplishments & Design Principles

### A. Centralized Buildkite Controller
To prevent race conditions where worker clusters compete to claim the same Buildkite job, the `agent-stack-k8s` controller runs **exclusively on the manager cluster**, and there is exactly **one** of it, on a single queue.

The Buildkite queue deliberately carries no TPU information. A step names its shape with `--profile`, and the controller stays shape-agnostic, so adding a TPU profile is a regenerated ConfigMap rather than another Helm release. The controller's own pod is CPU-only and carries no Kueue queue label, so Kueue never queues or evicts it - only the workload it submits.

### B. MultiKueue Dispatch over Connect Gateway
Kueue on the manager cluster inspects submitted jobs, matches cluster queues (`v6e-1-1x1`, `v6e-8-2x4`), and dispatches the workload object to the corresponding worker cluster (`southamerica-west1-a`) via GKE Connect Gateway using Workload Identity (`roles/gkehub.gatewayAdmin`).

### C. Native Cross-Project Image Pulling (No K8s Secrets Required)
Container images for testing (such as `us-central1-docker.pkg.dev/cloud-ullm-inference-ci-cd/tpu-inference-ci/vllm-tpu`) are hosted in the manager project. 

Terraform codifies cross-project IAM reader permissions (`roles/artifactregistry.reader`) for worker node service accounts (`tpu-ci-wkr-node@cloud-tpu-inference-test.iam.gserviceaccount.com`) on the manager project. GKE node containerd runtimes authenticate natively via GCP metadata tokens without requiring manual Kubernetes `imagePullSecrets` or service account keys.

### D. TPU is the only resource under quota
Buildkite workloads generate helper containers (e.g. `copy-agent`), initContainers (`imagecheck`) and the gcsfuse sidecar, all of which request CPU and memory. Kueue's default `quotaCheckStrategy: BlockUndeclared` refuses to admit a workload that requests a resource its ClusterQueue does not cover, so an earlier version of the queues carried `cpu` and `memory` in `coveredResources` with quotas of `10000` / `10000Gi` as a stand-in for "unbounded" - a `nominalQuota` of `0` is a real zero, not unlimited.

Both controller configs now set `resources.quotaCheckStrategy: IgnoreUndeclared` (Kueue 0.19, feature gate on by default), under which only the resources a ClusterQueue declares are checked. `queue_group.yaml.tpl` covers `google.com/tpu` alone; CPU and memory are enforced by the kube scheduler against node capacity, which is where they belong. Manager and workers must carry the same setting, since the worker's Kueue admits the mirrored workload too.

### E. Standardized Node Pool Configuration
TPU node pools in `clusters.tf` are configured with:
- **Taints**: `google.com/tpu=present:NoSchedule` (allows GKE Cluster Autoscaler to simulate scale-up for pending TPU pods).
- **Reservation Affinity**: `reservation_affinity` targeting designated Cloud TPU reservations.
- **Placement by workload, not by flavor**: the `ResourceFlavor` is per-accelerator (`v6e`) and carries no `nodeLabels`, so every profile in the cohort shares one flavor and can borrow from it. Kueue therefore injects no node selector, and the submitted workload names `accelerator_label` and `topology` itself. That is what makes a 1-chip job and an 8-chip job draw on the same pool of chips.

### F. JobSet for multi-pod workloads
A `batch/v1` Job cannot span hosts, so multi-host slices and prefill/decode disaggregation need JobSet. The operator is installed on the manager and every worker from a single `jobset_version`, and `jobset.x-k8s.io/jobset` is enabled in `integrations.frameworks` on both sides - MultiKueue mirrors the object across clusters, so the CRD, the operator version and the enabled framework list all have to match.

A pool with `hosts > 1` is a multi-host TPU slice pool: GKE creates it with a `COMPACT` placement policy carrying the topology, one node per host, and the autoscaler scales it atomically - every host or none. `clusters.tf` checks that such a pool's node counts are whole slices and that one pool is one slice (`max_nodes == hosts`); more slices of a shape are more pools. A four-host `ct6e-standard-4t` 4x4 slice (16 chips, a JobSet with `parallelism 4`) has been run this way; none is declared in the tfvars today.

### G. Physical shapes constrain borrowing
Node pools are single-shape, so with an 18-chip reservation one 8-chip node and ten 1-chip nodes leave nothing for a 16-chip slice. Quota borrowing across shapes is therefore not free: reclaiming chips means draining and deleting nodes of one shape before nodes of the other can be created. Measured on this cluster, that costs roughly 300s on top of the ~110s node scale-up. Worth knowing before tuning quotas - the cost is physical, not a Kueue setting.

### H. The workload launcher
Every TPU step runs `/opt/launcher/launch`, a CPU-only pod that submits the real workload and owns its lifecycle. It exists because agent-stack-k8s can only create a `batch/v1` Job; routing single-pod work through it as well keeps one code path and, more importantly, keeps the Buildkite agent *outside* the Kueue workload. That is what lets a preempted run pause and resume instead of failing, and stops the Buildkite job sitting reserved through a node scale-up.

Cluster-side it is a ServiceAccount, a Role, two ConfigMaps and a PodTemplate, all in `06-launcher.yaml`, plus a ClusterRole per worker (`07-launcher-rbac.yaml`) letting it read pod logs over Connect Gateway.

The split of responsibility: the **profile registry** is generated here from the same tfvars that builds the node pools, so chip count, topology, node labels and queue names cannot drift from the queues they target. The **workload manifest** lives in the repo under test, so the shape of a job is a PR rather than an infrastructure change. A pipeline names a profile; it cannot invent placement.

Two behaviours worth knowing when reading the launcher:
- Pod logs are **polled**, not followed. Connect Gateway resets the long-lived HTTP/2 stream `kubectl logs -f` needs (`stream error ... INTERNAL_ERROR`), while short requests through it are reliable.
- Logs are filed **per owning Job**, not per pod, and uploaded as Buildkite artifacts. A pod does not survive preemption; the Job does, so keying on it gives one continuous log across a preempt-and-resume cycle.

---

## 3. Workflow & Usage

### Modifying Configuration
All cluster topology and pool limits are declared in `prod.auto.tfvars`.

Example pool definition:
```hcl
tpu_pools = {
  v6e-1-1x1 = {
    machine_type      = "ct6e-standard-1t"
    accelerator       = "v6e"          # names the cohort and the ResourceFlavor
    accelerator_label = "tpu-v6e-slice"
    topology          = "1x1"
    chips_per_node    = 1
    min_nodes         = 2              # kept warm
    nominal_nodes     = 10             # Kueue nominalQuota = chips x nominal_nodes
    max_nodes         = 10             # autoscaling ceiling; anything above nominal is borrowed
    reservation_name  = "cloudtpu-20250327121505-861300654"
  }
  # a slice across hosts would add `hosts = 4` on a ct6e-standard-4t 4x4 pool;
  # one multi-host pool is one slice and max_nodes must equal hosts.
}
```

The pool key is the profile name, and it is used verbatim as the Kueue
ClusterQueue and LocalQueue name and as the `--profile` a pipeline passes.
`nominal_nodes` is what the profile owns; anything between it and `max_nodes`
is borrowed from the cohort and is the first thing reclaimed when another
profile needs its own quota back.

### Manifest Generation

```bash
python3 -m pip install -r scripts/requirements.txt   # hcl2, once
python3 scripts/generate_manifests.py
```

Manifests are rendered into per-cluster directories, numbered in the order
`kubectl` applies them:

```
generated/manager/            01-base 02-multikueue-fleet 03-cohorts
                              04-resource-flavors 05-queues [06-launcher]
generated/worker-<location>/  01-base 02-resource-flavors 03-queues
                              [07-launcher-rbac]
```

Only `01-base` ordering is load-bearing - it carries the Namespace everything
else is created into. The rest is soft: a ClusterQueue naming a ResourceFlavor
that does not exist yet goes inactive and recovers when it appears.

`generated/` is committed, so regenerating should produce no diff unless you
changed a template or the tfvars. A non-empty diff after an unrelated change
means something drifted.

### Applying Infrastructure & Manifests

```bash
terraform fmt && terraform apply
./scripts/deploy_manifests.sh
```

`deploy_manifests.sh` applies each cluster's directory in one call. To land
them one at a time when debugging a fresh install:

```bash
for f in generated/manager/*.yaml; do echo "== $f"; kubectl apply -f "$f" || break; done
```

### Verifying a deploy

Against the **manager**:

```bash
kubectl -n buildkite get localqueue
kubectl get crd jobsets.jobset.x-k8s.io
kubectl -n buildkite get pods -l app.kubernetes.io/name=agent-stack-k8s
```

Expect LocalQueues matching the profile names, the JobSet CRD, and exactly one
agent-stack pod - more than one means an older per-profile controller survived
and will compete for jobs.

### Attaching to a running workload

MultiKueue decides which worker a job lands on, so the person who submitted it
does not know where its pods are - and a worker's pods are invisible from the
manager, since MultiKueue skips pod creation there. `scripts/mkexec.sh` closes
that gap by asking the manager, which is the component that made the decision:

```
job name ──► Workload            (ownerReferences)
         ──► worker cluster      (Workload .status.clusterName)
         ──► ClusterProfile      (MultiKueueCluster .spec.clusterSource)
         ──► Connect Gateway URL (ClusterProfile .status.accessProviders)
```

```bash
scripts/mkexec.sh <job-name>                      # interactive shell
scripts/mkexec.sh <job-name> -- cat /etc/hostname # one-shot command
scripts/mkexec.sh <job-name> -i 2 -c main         # third pod of a JobSet
```

The caller names a job and the manager context; the worker is never typed. It
is an ordinary CLI, so a workstation or a plain VM with `gcloud` configured
works the same way - nothing about it needs to run inside a cluster. A
throwaway kubeconfig is synthesized per invocation and the caller's own
credentials are used, so no shared worker kubeconfig is distributed and the
user's real kubeconfig is left untouched.

`scripts/mkexec-demo-queues.yaml` and `scripts/mkexec-demo-job.yaml` exercise
the whole path on a CPU-only queue that rides the same MultiKueue admission
check, so the resolver can be demonstrated without spending TPU.

#### Access required

**The terraform in this directory does not provision human access to the
workers.** It grants Connect Gateway only to the Kueue controller
(`gatewayEditor`) and the launcher (`gatewayReader`). An operator needs both of
the following before `mkexec.sh` can reach a pod, or it stops at a 403:

1. **GCP IAM.** `exec` is a `create` on `pods/exec`, a write verb, so
   `gatewayReader` is not sufficient:

   | role | project |
   |---|---|
   | `roles/gkehub.gatewayEditor` | each worker project |
   | `roles/gkehub.viewer` | the manager project, to resolve Fleet memberships |

2. **Kubernetes RBAC on each worker.** Over Connect Gateway the subject is the
   caller's identity, not a ServiceAccount, so bind a group rather than
   maintaining a list of emails:

   ```yaml
   apiVersion: rbac.authorization.k8s.io/v1
   kind: ClusterRole
   metadata:
     name: tpu-workload-debugger
   rules:
     - apiGroups: [""]
       resources: ["pods", "pods/log"]
       verbs: ["get", "list", "watch"]
     - apiGroups: [""]
       resources: ["pods/exec"]
       verbs: ["create"]
   ---
   apiVersion: rbac.authorization.k8s.io/v1
   kind: ClusterRoleBinding
   metadata:
     name: tpu-workload-debugger
   roleRef:
     apiGroup: rbac.authorization.k8s.io
     kind: ClusterRole
     name: tpu-workload-debugger
   subjects:
     - kind: Group
       name: tpu-ci-debuggers@your-domain.example
       apiGroup: rbac.authorization.k8s.io
   ```

   This is deliberately not generated into `generated/`: who may exec into a
   shared CI cluster is an access-control decision, not a topology one, and it
   should not follow the same "regenerate and apply" path as the queues.

On the manager the caller additionally needs read on `workloads`,
`multikueueclusters` and `clusterprofiles` - the four lookups above.

#### Holding a TPU for development

A long-running job is a workable way to claim a chip and develop against it,
but note that the v6e lanes in `prod.auto.tfvars` are sized `nominal == max`
and sum to the whole 18-chip reservation. There is no slack, so a claim job on
those queues takes chips straight out of CI. Before pointing this at v6e, give
it a ClusterQueue in the same cohort with `nominalQuota: 0` - it can then only
borrow what is idle, and `reclaimWithinCohort: Any` hands the chips back when
CI needs them - and set `maximumExecutionTimeSeconds` so a session forgotten on
a Friday releases itself. Preemption kills the pod and therefore the shell;
that is the tradeoff for not reserving capacity.

---

## 4. Best Practices & Troubleshooting

### Build Cancellation
Always cancel builds via the **Buildkite UI** or Buildkite CLI (`buildkite-agent build cancel <build-id>`). Avoid running manual `kubectl delete job` directly out-of-band, as deleting `Job` objects bypasses the controller event watcher and leaves Buildkite builds pending until step timeouts expire.

### Pipeline Timeouts
In `.buildkite/pipeline_kube.yaml`, ensure pipeline steps include `timeout_in_minutes: N` and `cancel_on_build_failing: true` so stalled steps fail fast automatically.
