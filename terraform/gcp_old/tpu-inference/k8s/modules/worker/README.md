# modules/worker

One TPU worker cluster for the kube CI fleet, built in your own project. The
cluster joins the fleet in `cloud-ullm-inference-ci-cd`; the fleet's manager
dispatches CI workloads to it, and the fleet's deploy installs what runs inside
it. You own the cluster and the project. You need no access to the fleet's
project, and the maintainers register the cluster in the fleet from their side
once it exists. In yours the fleet gets what registration needs and read access
for its dashboard, which your apply grants.

Your workers are module blocks in an environment, `workers/<environment>/`:
one Terraform root and one state, for one worker or several.
`workers/cloud-ullm-inference-ci-cd/` is the fleet's own and a working example.

## What it creates

All of it in your project:

- A regional GKE Standard cluster, `tpu-ci-<name>`, registered to the fleet.
- A small system node pool, one node pool per TPU shape drawing on your
  reservation, and a Spot pool for a shape that asks for one.
- The nodes' service account, `tpu-ci-wkr-<name>`, with GKE's default node
  role and nothing else.
- Two cache buckets in the cluster's region, one for compilation and one for
  models, which the CI workloads read and write, and a Rapid Cache in front of
  each in the zones you name.
- If your project is not `cloud-ullm-inference-ci-cd`, `roles/gkehub.serviceAgent`
  and `roles/gkehub.crossProjectServiceAgent` for the fleet's GKE Hub service
  agent, which the maintainers' registration of the cluster needs, and in the
  cluster a binding of the fleet's deployer account,
  `tpu-ci-deployer@cloud-ullm-inference-ci-cd.iam.gserviceaccount.com`, to
  `cluster-admin`. See "Why the deployer binding".
- If your project is not `cloud-ullm-inference-ci-cd`, read access for the
  fleet's dashboard account,
  `tpu-ci-dashboard@cloud-ullm-inference-ci-cd.iam.gserviceaccount.com`:
  `roles/container.clusterViewer` and `roles/monitoring.viewer` on the
  project, and `roles/logging.viewAccessor` on the `_Default` bucket's
  `_AllLogs` view. The cluster's node pools, metrics, events and container
  logs are kept in your project, and the dashboard shows them beside the
  fleet's other clusters.

## Before you apply

In your project:

- The TPU reservation, in the zone your pools name. Nodes consume it as a
  specific reservation.
- The `container`, `compute`, `iam`, `storage` and `secretmanager` APIs.
- A subnet in the cluster's region with Private Google Access, and a `/28` for
  the control plane that nothing else in the network uses.
- A Cloud NAT for that region and network. The nodes are private, and Kueue
  and JobSet are pulled from registry.k8s.io.
- A GCS bucket for the Terraform state.
- Permission to create all of the above, which is what applying runs as.

## What you do not provide

No secrets and no tokens:

- The credentials a workload asks for (`HF_TOKEN`, `BUILDKITE_ANALYTICS_TOKEN`)
  stay in the fleet's Secret Manager. GKE's SecretSync copies them into your
  cluster, reading as the cluster's own Workload Identity, which the fleet
  grants once your worker is listed.
- The Buildkite agent token and the git deploy key stay on the manager.
- Each job's Buildkite access token is passed to its pods by the launcher, and
  is good for that one job.
- CI images, buckets and BigQuery tables are the fleet's, and the fleet grants
  your nodes and workloads access to them.

## Applying

The fleet's maintainers send a branch with your environment: `main.tf` with
the backend, the locals your workers share and the kubernetes provider, and a
`<name>.tf` per worker with its module block filled in from your project,
region, network, control plane `/28`, reservation and shapes. Then:

```bash
terraform -chdir=workers/<environment> init
terraform -chdir=workers/<environment> apply
```

Keep every argument in a module block a literal, or one of the environment's
locals. The fleet's generator reads them to build the Kueue queues for your
shapes, and it cannot evaluate an expression.

The apply talks to the new cluster once, to create the deployer binding, so it
has to reach the cluster's endpoint, which is public and authenticated. The
kubernetes provider in `main.tf` is what it uses:

```hcl
data "google_client_config" "default" {}

provider "kubernetes" {
  host                   = "https://${module.<name>.endpoint}"
  cluster_ca_certificate = base64decode(module.<name>.ca_certificate)
  token                  = data.google_client_config.default.access_token
}
```

With more than one worker in the environment, give each its own provider
alias and pass it in the module block as `providers = { kubernetes = ... }`.

Once it is applied, the maintainers list the worker in the fleet and deploy
to it. Nothing else is needed from you.

## Why the deployer binding

Terraform stops at the cluster. What runs inside it - Kueue, JobSet, the
queues and their quota, the RBAC the manager dispatches through, the secret
syncs, the cache volumes - is installed and upgraded by the fleet's deploy,
because it changes with every quota change and every Kueue or launcher
release. The deploy runs as the deployer account and reaches your API server
through Connect Gateway, which passes that identity through. The account holds
no role in your project, so in your cluster only RBAC can authorize it, and
the binding is that RBAC. It grants nothing outside the cluster, and deleting
it shuts the fleet's deploy out. Which people may act as the account is the
fleet's to decide.

## Afterwards

- **A pool change** - a new shape, `max_nodes`, `slices`, a reservation - is
  an edit to the worker's module block. You apply it; the maintainers
  regenerate and deploy.
- **A quota change** - `nominal_quota`, `reclaim_within_cohort`,
  `queueing_strategy`, `cpu_workloads` - is read only by the fleet's generator.
  Your plan shows no change, and the maintainers deploy it without you.
- **A Kueue, JobSet or launcher change** needs nothing from you.
- **CPU-only steps** run on your cluster only if its module block sets
  `cpu_workloads = true`, since the nodes they create are billed to your
  project. It is off by default.
- **What you can see:** as the cluster's administrators, everything on it,
  including the credentials copied there and each job's Buildkite access
  token while the job runs.
