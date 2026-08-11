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

### D. Kueue Resource Quotas & InitContainers (`coveredResources`)
Buildkite workloads generate helper containers (e.g. `copy-agent`) and initContainers (`imagecheck`) that request CPU and memory. 

To prevent Kueue from halting flavor allocation for workloads with CPU/memory requests, both `google.com/tpu`, `cpu` (quota: `10000`), and `memory` (quota: `10000Gi`) are explicitly included under `coveredResources` in `queue_group.yaml.tpl`.

### E. Standardized Node Pool Configuration
TPU node pools in `clusters.tf` are configured with:
- **Taints**: `google.com/tpu=present:NoSchedule` (allows GKE Cluster Autoscaler to simulate scale-up for pending TPU pods).
- **Reservation Affinity**: `reservation_affinity` targeting designated Cloud TPU reservations.
- **Placement by workload, not by flavor**: the `ResourceFlavor` is per-accelerator (`v6e`) and carries no `nodeLabels`, so every profile in the cohort shares one flavor and can borrow from it. Kueue therefore injects no node selector, and the submitted workload names `accelerator_label` and `topology` itself. That is what makes a 1-chip job and an 8-chip job draw on the same pool of chips.

### F. JobSet for multi-pod workloads
A `batch/v1` Job cannot span hosts, so multi-host slices and prefill/decode disaggregation need JobSet. The operator is installed on the manager and every worker from a single `jobset_version`, and `jobset.x-k8s.io/jobset` is enabled in `integrations.frameworks` on both sides - MultiKueue mirrors the object across clusters, so the CRD, the operator version and the enabled framework list all have to match.

### G. Physical shapes constrain borrowing
Node pools are single-shape, so with a 10-chip reservation one 8-chip node leaves room for at most two 1-chip nodes. Quota borrowing across shapes is therefore not free: reclaiming chips means draining and deleting nodes of one shape before nodes of the other can be created. Measured on this cluster, that costs roughly 300s on top of the ~110s node scale-up. Worth knowing before tuning quotas - the cost is physical, not a Kueue setting.

---

## 3. Workflow & Usage

### Modifying Configuration
All cluster topology and pool limits are declared in [`prod.auto.tfvars`](file:///Users/mhhua/repos/ci-infra/terraform/gcp_old/tpu-inference/k8s/prod.auto.tfvars).

Example pool definition:
```hcl
tpu_pools = {
  v6e-1-1x1 = {
    machine_type      = "ct6e-standard-1t"
    accelerator       = "v6e"          # names the cohort and the ResourceFlavor
    accelerator_label = "tpu-v6e-slice"
    topology          = "1x1"
    chips_per_node    = 1
    min_nodes         = 0
    nominal_nodes     = 2              # Kueue nominalQuota = chips x nominal_nodes
    max_nodes         = 10             # autoscaling ceiling; the rest is borrowed
    reservation_name  = "cloudtpu-20250327121505-861300654"
  }
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
                              [04-launcher-rbac]
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

---

## 4. Best Practices & Troubleshooting

### Build Cancellation
Always cancel builds via the **Buildkite UI** or Buildkite CLI (`buildkite-agent build cancel <build-id>`). Avoid running manual `kubectl delete job` directly out-of-band, as deleting `Job` objects bypasses the controller event watcher and leaves Buildkite builds pending until step timeouts expire.

### Pipeline Timeouts
In `.buildkite/pipeline_kube.yaml`, ensure pipeline steps include `timeout_in_minutes: N` and `cancel_on_build_failing: true` so stalled steps fail fast automatically.
