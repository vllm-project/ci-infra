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
To prevent race conditions where worker clusters compete to claim the same Buildkite job, the `agent-stack-k8s` controller runs **exclusively on the manager cluster**. The manager controller creates Kubernetes `Job` resources in the `buildkite` namespace on the manager cluster.

### B. MultiKueue Dispatch over Connect Gateway
Kueue on the manager cluster inspects submitted jobs, matches cluster queues (`v6e-1`, `v6e-8`), and dispatches the workload object to the corresponding worker cluster (`southamerica-west1-a`) via GKE Connect Gateway using Workload Identity (`roles/gkehub.gatewayAdmin`).

### C. Native Cross-Project Image Pulling (No K8s Secrets Required)
Container images for testing (such as `us-central1-docker.pkg.dev/cloud-ullm-inference-ci-cd/tpu-inference-ci/vllm-tpu`) are hosted in the manager project. 

Terraform codifies cross-project IAM reader permissions (`roles/artifactregistry.reader`) for worker node service accounts (`tpu-ci-wkr-node@cloud-tpu-inference-test.iam.gserviceaccount.com`) on the manager project. GKE node containerd runtimes authenticate natively via GCP metadata tokens without requiring manual Kubernetes `imagePullSecrets` or service account keys.

### D. Kueue Resource Quotas & InitContainers (`coveredResources`)
Buildkite workloads generate helper containers (e.g. `copy-agent`) and initContainers (`imagecheck`) that request CPU and memory. 

To prevent Kueue from halting flavor allocation for workloads with CPU/memory requests, both `google.com/tpu`, `cpu` (quota: `10000`), and `memory` (quota: `10000Gi`) are explicitly included under `coveredResources` in `queue_group_manager.yaml.tpl` and `queue_group_worker.yaml.tpl`.

### E. Standardized Node Pool Configuration
TPU node pools in `clusters.tf` are configured with:
- **Taints**: `google.com/tpu=present:NoSchedule` (allows GKE Cluster Autoscaler to simulate scale-up for pending TPU pods).
- **Reservation Affinity**: `reservation_affinity` targeting designated Cloud TPU reservations.
- **Declarative Node Labels**: Explicitly passed via `node_labels` in `prod.auto.tfvars` (e.g. `cloud.google.com/gke-tpu-accelerator` and `cloud.google.com/gke-tpu-topology`).

---

## 3. Workflow & Usage

### Modifying Configuration
All cluster topology and pool limits are declared in [`prod.auto.tfvars`](file:///Users/mhhua/repos/ci-infra/terraform/gcp_old/tpu-inference/k8s/prod.auto.tfvars).

Example pool definition:
```hcl
tpu_pools = {
  v6e-1 = {
    machine_type     = "ct6e-standard-1t"
    chips_per_node   = 1
    min_nodes        = 0
    max_nodes        = 16
    reservation_name = "cloudtpu-20250327121505-861300654"
    cohort           = "v6e-cohort"
    node_labels = {
      "cloud.google.com/gke-tpu-accelerator" = "tpu-v6e-slice"
      "cloud.google.com/gke-tpu-topology"    = "1x1"
    }
  }
}
```

### Manifest Generation
To generate updated Kueue manifests from templates:
```bash
python3 scripts/generate_manifests.py
```
This script purges stale artifacts and renders fresh manifests into `generated/`:
- `generated/manager.yaml` (Manager MultiKueue ClusterQueues & ResourceFlavors)
- `generated/worker-<location>.yaml` (Worker LocalQueues & ResourceFlavors)

### Applying Infrastructure & Manifests
1. **Apply Terraform**:
   ```bash
   terraform fmt
   terraform apply
   ```
2. **Apply Generated Kueue Manifests**:
   - Manager Cluster:
     ```bash
     gcloud container clusters get-credentials tpu-ci-manager --location us-central1 --project cloud-ullm-inference-ci-cd
     kubectl apply -f generated/manager.yaml
     ```
   - Worker Cluster:
     ```bash
     gcloud container clusters get-credentials tpu-ci-southamerica-west1-a --location southamerica-west1-a --project cloud-tpu-inference-test
     kubectl apply -f generated/worker-southamerica-west1-a.yaml
     ```

---

## 4. Best Practices & Troubleshooting

### Build Cancellation
Always cancel builds via the **Buildkite UI** or Buildkite CLI (`buildkite-agent build cancel <build-id>`). Avoid running manual `kubectl delete job` directly out-of-band, as deleting `Job` objects bypasses the controller event watcher and leaves Buildkite builds pending until step timeouts expire.

### Pipeline Timeouts
In `.buildkite/pipeline_kube.yaml`, ensure pipeline steps include `timeout_in_minutes: N` and `cancel_on_build_failing: true` so stalled steps fail fast automatically.
