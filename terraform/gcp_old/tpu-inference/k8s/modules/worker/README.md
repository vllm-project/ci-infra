# modules/worker

One TPU worker cluster for the kube CI fleet, built in your own project. The
cluster joins the fleet in `cloud-ullm-inference-ci-cd`; the fleet's manager
dispatches CI workloads to it, and the fleet's deploy installs what runs inside
it. You own the cluster and the project. You need no access to the fleet's
project beyond one temporary grant, and the fleet needs no role in yours.

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
  for the fleet's GKE Hub service agent, which registering the cluster needs.

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

Send the fleet's maintainers the identity you will apply as. They grant it
`roles/gkehub.editor` on `cloud-ullm-inference-ci-cd` for as long as the build
takes, because GKE creates the cluster's fleet membership there as whoever
creates the cluster.

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
the backend and the locals your workers share, and a `<name>.tf` per worker
with its module block filled in from your project, region, network, control
plane `/28`, reservation and shapes. Then:

```bash
terraform -chdir=workers/<environment> init
terraform -chdir=workers/<environment> apply
```

Keep every argument in a module block a literal, or one of the environment's
locals. The fleet's generator reads them to build the Kueue queues for your
shapes, and it cannot evaluate an expression.

## After applying

Bind whoever deploys for the fleet, which the maintainers will name:

```bash
kubectl create clusterrolebinding tpu-ci-deploy-<user> --clusterrole=cluster-admin --user=<user>@google.com
```

Terraform stops at the cluster. What runs inside it - Kueue, JobSet, the
queues and their quota, the RBAC the manager dispatches through, the secret
syncs, the cache volumes - is installed and upgraded by the fleet's deploy,
because it changes with every quota change and every Kueue or launcher
release. The deploy reaches your API server through Connect Gateway as the
person running it, and in your cluster only RBAC can authorize them, since
they hold no role in your project.

The maintainers then list the worker in the fleet and deploy to it.

## Afterwards

- **A pool or quota change** is an edit to the worker's module block. You
  apply it; the maintainers regenerate and deploy.
- **A Kueue, JobSet or launcher change** needs nothing from you.
- **What you can see:** as the cluster's administrators, everything on it,
  including the credentials copied there and each job's Buildkite access
  token while the job runs.
