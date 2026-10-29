# tpu-inference CI on GKE

TPU steps for the `tpu-inference` suite, run as Kubernetes workloads instead of
on long-lived agent VMs. A Buildkite step on queue `kube` becomes a Kueue
workload, which is admitted against fleet quota on a manager cluster and
dispatched to whichever worker cluster has the chips.

One manager and one worker per reservation, and that split is the whole design:

- **Manager** — `tpu-ci-manager`, Standard, `us-central1`. No TPUs. Runs the
  agent-stack-k8s controller, the Buildkite agent pods, and the Kueue that owns
  fleet-wide quota. This is where a step's agent lives and where its log goes.
  It declares no node pools: nodes are auto-provisioned per pending pod, from
  the machine families the `manager-system` ComputeClass lists in order.
- **Worker** — `tpu-ci-us-east5`, Standard, `us-east5`. v6e, as
  `ct6e-standard-1t` (1x1) and `ct6e-standard-8t` (2x4).
- **Worker** — `tpu-ci-us-central1`, Standard, `us-central1`. v7x, as
  `tpu7x-standard-1t` (1x1x1, with a Spot pool beside the reserved one) and
  `tpu7x-standard-4t` (2x2x1, and multi-host 2x2x2 across two VMs and 2x2x4
  across four). Every v7x step in the fleet runs here.

How many chips each shape gets is in the worker's module block under
`workers/`; read it there rather than from a doc.

A worker reports to the manager over Connect Gateway and runs no agent of its
own. MultiKueue joins them: the manager admits, the worker executes. An operator
talks to the manager for almost everything.

Each worker also carries a `worker-cpu` ComputeClass for roles that hold no
chips, and the fleet's secrets, put there by GKE SecretSync — a workload pod
runs on the worker, so a `secretKeyRef` has to resolve there rather than on the
manager where the launcher built the podspec.

A CPU-only workload - `launch --cpu`, or a manifest asking for no TPU - goes to
the manager's `cpu` queue, which hands it to whichever worker admits it first.
Only workers whose module block sets `cpu_workloads = true` are on that list,
since the nodes it creates are billed to the worker's project: ours are, a
partner's is not unless agreed. A chip-less role inside a TPU workload is
different, and runs beside its TPU roles on any worker.

## Layout

| Path | What it is |
| --- | --- |
| `AGENTS.md` | How to work on the fleet: access, where to change what, deploying, rules. Start here. |
| `manager/` | The manager's Terraform root: the manager cluster, the dashboard, and every grant in `cloud-ullm-inference-ci-cd`, including the ones each worker needs. Terraform owns infrastructure only. |
| `manager/prod.auto.tfvars` | The fleet: which workers it has, versions, launcher timeouts, the secrets workloads may ask for. |
| `workers/<environment>/` | One Terraform root and state per environment, applied by whoever administers it; ours is `cloud-ullm-inference-ci-cd`, with both workers. Each worker is a module block calling `modules/worker`, in a file of its own name, and the shapes, quotas and reservations in it are the only place they are written. See "Add a worker". |
| `modules/worker/` | One worker cluster: its node pools, node service account and cache buckets, all in the worker's own project. Its README is what an environment's administrators need. |
| `scripts/generate_manifests.py` | Renders `kueue/generated/` from `manager/prod.auto.tfvars` and the module block in each listed worker's `main.tf`. |
| `scripts/deploy_manifests.py` | Installs Kueue and JobSet, applies `kueue/generated/`. |
| `kueue/templates/` | The templates the generator renders. |
| `kueue/generated/` | The YAML that actually gets applied. Committed on purpose — see below. |
| `kueue/launcher/` | The program every TPU step runs, its Job, and its image build. |
| `kueue/launcher/pod_defaults.yaml` | What the fleet gives a workload's pods: caches, gcsfuse settings, eviction and retry policy. One definition, inherited by the built-in Job and by every manifest. |
| `manager/dashboard.tf`, `dashboard/` | The fleet's health dashboard: a Cloud Run service, its program (`app.py` serves, `fleet.py` reads, `views.py` and `charts.py` render), templates, static files and image build. |

Terraform stops at the cluster; `deploy_manifests.py` starts there. The
Kubernetes and Helm providers need a reachable API server at plan time, which
would make creating a cluster and configuring it two runs with a hand-edited
variable in between. The one exception is a worker in another project, whose
own apply binds the fleet's deployer account to `cluster-admin`
(`modules/worker/deploy.tf`): nothing can be deployed there until that binding
exists, and whoever applies the worker is the one party certain to be able to
create it.

`kueue/generated/` is committed so that what a deploy applies is exactly what
is in git, and so that a change's effect on it can be read when it matters.
`deploy_manifests.py` refuses to run if the committed tree differs from a fresh
render, so the tree is the render of the inputs and nothing else, and it doubles
as a drift detector - but only between the templates and the committed tree.
That is why a review is of the inputs - `manager/prod.auto.tfvars`, the module
blocks under `workers/`, the templates and the generator - and why GitHub
collapses this tree in pull requests (`.gitattributes`); expand a file when the
inputs' effect on it is the question.

The deploy applies, never prunes, so **deleting an object from the tree does
not delete it from the clusters**: a change that removes a queue, a flavor or an
admission check needs the matching `kubectl delete` by hand, on the manager and
on every worker that carried it. The one exception is a worker's
MultiKueueCluster on the manager, which the deploy deletes once the worker
leaves `workers` - see "Pause or remove a worker".

## Deploying a change

```bash
pip install -r scripts/requirements.txt

terraform -chdir=workers/<env> apply       # if a worker there or modules/worker changed
terraform -chdir=manager apply             # if manager/ changed, or a worker joined or left
./scripts/generate_manifests.py            # if a worker's shapes, manager/prod.auto.tfvars or a template changed
./scripts/deploy_manifests.py              # diff, confirm, apply
```

The deploy runs as `tpu-ci-deployer@cloud-ullm-inference-ci-cd.iam.gserviceaccount.com`,
through gcloud impersonation, on every cluster: our own let it in by its IAM
on this project, and a worker elsewhere by the binding its apply creates. To
run it you have to be in `deployers` in `manager/prod.auto.tfvars`.

`deploy_manifests.py --mode diff` shows what would change and stops;
`--mode apply` skips the preview and the prompt. It walks the manager first so
its queues exist before a worker reports to them, and it uses its own temporary
kubeconfig, so it will not touch yours or leave a context selected.

Deploying does not disturb a workload that is already running. Every deploy
rolls `kueue-controller-manager` whether or not its config changed, which looks
like it would, and it does not: Kueue gates admission and nothing else, so once
a workload is running its pods belong to `Job` objects driven by the
control-plane Job controller and `jobset-controller-manager`, neither of which
the deploy touches. Measured against a live benchmark - the controller was
replaced while pod names, start times, restart counts and `Admitted` on both
manager and worker all stayed as they were.

What the roll does affect is pod creation, for the seconds it takes: see "A
deploy briefly rejects pod creation, cluster-wide" below. So the case to think
about before deploying is not a workload that is running, but one that is about
to start a pod.

Always commit the regenerated `kueue/generated/` alongside whatever produced it.
A change to a comment in a template counts: the comments are rendered into the
ConfigMaps.

## Running a step

Every TPU step invokes one program. The common case is a single pod, and the
step names the hardware:

```yaml
- label: "unit tests"
  agents: { queue: kube }
  command: launch --machine-type ct6e-standard-8t --topology 2x4 -- pytest tests/
```

Anything else — multi-host, disagg, more than one role — brings a manifest, and
states its hardware inside it:

```yaml
  command: launch --manifest .buildkite/kubernetes/manifests/1p1d.yaml
```

and passes nothing else. Not the shape, because a JobSet already says where each
of its pods runs, which no pair of flags can express. Not the command either — a
JobSet has one per role. A manifest passed together with a command is refused
rather than one role being silently chosen.

### What a manifest states, and what it inherits

A manifest states the hardware it wants and nothing that follows from it:

```yaml
metadata:
  annotations:
    tpu-ci.google.com/defaults: standard
```

With that annotation the launcher merges in `pod_defaults.yaml` — the cache
volumes and their mounts, the gcsfuse sidecar settings, the TPU toleration, the
service account, `restartPolicy`, the two env names every workload wants, the
TTL, and the retry rules that let a pod survive its node being repaired. A
JobSet's multi-host roles also get JobSet's exclusive placement, one slice per
Job. The memory request comes from the shape's profile, since it is a fraction
of the host the pod landed on.

The merge is additive: anything the manifest sets itself is left alone, so a
role can add a volume or override a default it needs to differ on. Inherited
mounts are applied first, so a mount nested inside an inherited one lands inside
it rather than under it.

Only roles that hold chips get the caches and retry rules — they are sized from
a TPU host's memory and about TPU nodes being repaired, and a CPU-only role runs
on neither. Such a role states what it needs itself.

A workload that reads its data once per run can go without the caches:

```yaml
metadata:
  annotations:
    tpu-ci.google.com/defaults: standard
    tpu-ci.google.com/caches: none
```

Its pods then get no cache claim, no `gke-gcsfuse-cache` volume and no mounts
for them; everything else in `pod_defaults.yaml` still applies, the memory
request included. The gcsfuse file cache is a memory-backed volume on the pod,
so it holds host memory for the whole run, and a run that reads each file once
never reads it back: a 1.5 TB checkpoint streamed at boot keeps 61-67 GiB of
every host's memory in it. The gcsfuse sidecar settings are left out too,
unless a pod mounts a gcsfuse volume of its own, which needs the sidecar; give
that volume no `fileCacheCapacity` and it has no file cache either. Without the
compilation cache, every run compiles from scratch into the container's own
disk.

An empty value is the same as leaving the annotation unset. That is for a
manifest shared by steps that want different answers: it writes
`tpu-ci.google.com/caches: "${SOME_VARIABLE}"`, and each step exports the
variable empty or as `none`. Any other value is refused.

What stays in the manifest is `nodeSelector` and the `google.com/tpu` count:
together with the chip count they are how the queue is chosen, so there would be
nothing left to resolve if the fleet supplied them. Its deadline stays too.

Two things a manifest should not set. A **CPU request** is a scheduling floor
checked against the template the autoscaler builds for a shape; one large enough
to matter can exceed what that template offers and stop the pool building nodes
at all. An **ephemeral-storage** request reserves a large share of a node's disk
to cap a pod that already holds every chip on it.

The image is `WORKLOAD_IMAGE` in the step's environment, checked against
`allowed_image_repos` — a CI image is built per commit, so which one runs is the
pipeline's choice, which in a public repo means a PR's. A tag is resolved to the
digest it points at when the step submits, so every pod in the workload and
every restart pull the same bytes even if the tag is republished mid-run.

Every role that holds chips must ask for the same shape: a workload is admitted
against one queue and a queue is one shape. A role that asks for no accelerator
at all is the exception and rides along — a benchmark client driving the servers
over HTTP, say — because the queues put `google.com/tpu` alone under quota. Give
such a role `nodeSelector: cloud.google.com/compute-class: worker-cpu` and real
CPU requests; otherwise it lands on the worker's small shared system pool, or on
a TPU node where it would sit on four chips to run a Python process. If it also
mounts `gke-gcsfuse-cache` or `dshm` it must state their `sizeLimit` itself —
the launcher sizes both from the TPU host's memory, which this node is not on.

A manifest must contain a container named `workload`: that is the one whose
output is streamed back and which step environment is forwarded to. More than
one may carry the name, and in a JobSet whose roles all want the log and the
step's secrets, they all should. The queue label is the launcher's alone and is
rejected in a manifest: a queue named here is either the shape said twice or a
disagreement with it. The gcsfuse cache and `/dev/shm` sizes are filled in only
where the manifest leaves them open, so a pod that needs the memory for itself
can say so.

How long the workload runs is not one of those. It defaults to
`tpu_test_max_seconds`, which is right for a test, and a manifest that knows
better states its own `activeDeadlineSeconds` — a serving benchmark runs for as
long as its client sweeps, which no shape implies. A single step can override
both by setting `TPU_MAX_RUNTIME_SECONDS` in its `env:`, which is how one step
asks for longer without every step sharing the manifest getting it too. The
ceiling is `tpu_runtime_max_seconds`: past that the workload would outlive the
launcher watching it, and the chips would be held by nothing.

Whatever the source, keep it under the step's own `timeout_in_minutes` by more
than the startup envelope. The two clocks do not start together — the step's
runs from the agent pod, the workload's from admission — so a workload given
the step's whole budget is killed by Buildkite before its own deadline can fire
or its artifacts can upload.

`kueue/launcher/launch.py` is the program. It is a file rather than YAML so it
can be linted and run; `deploy_manifests.py` builds the ConfigMap from it.

## How the launcher works

The launcher runs once per step, in the agent pod's `launcher` container, and
lasts as long as the step. In order:

1. **Resolve.** Read the profile registry (`/opt/launcher/profiles/profiles.yaml`,
   from the `tpu-launcher-profiles` ConfigMap that `generate_manifests.py`
   renders out of the fleet's Terraform). Render the manifest or the built-in
   `job.yaml`, merge `pod_defaults.yaml`, and read the shape back out of the
   pods to choose the queue. Validation, pinning the image to a digest, the
   priority class (the step's `WORKLOAD_PRIORITY`, else the build's
   `WORKLOAD_PRIORITY` meta-data), env forwarding and the deadline are settled
   here, before anything is submitted.
2. **Submit.** Create the Job or JobSet `bk-<job uuid without dashes>` in the
   manager's `buildkite` namespace, owned by the agent pod and labelled
   `buildkite.com/job-id`. A step at or below `tpu_spot_max_priority` keeps a
   copy without the Spot exclusion for later.
3. **Watch**, every 5 seconds until the workload ends:
   - not yet admitted: annotate the build with Kueue's reason; resubmit if
     MultiKueue dropped the dispatch; after `tpu_spot_after_seconds`, resubmit
     pinned to Spot if the step may use it; give up at the queue or dispatch
     budget (`not_admitted`);
   - admitted: get credentials for the worker, read the workload's pods there
     and stream the `workload` containers' output into the step log;
   - a Spot pod with no node, or whose node was reclaimed: resubmit off Spot;
   - a pod evicted for its disk use: fail (`evicted_storage`);
   - a pod no node of its shape can hold: warn, then fail (`unfit`);
   - the workload finished: take the exit code from its pods.
4. **Record.** Print one JSON line with the workload's timing and outcome and
   insert it into `ci_efficiency_metrics.kube_workload_timing`, which the
   dashboard's Jobs and Review pages read.

On a Buildkite cancel, the agent pod gets SIGTERM and the launcher starts
deleting the workload, but its container is usually killed within the same
second. The workload is owned by the agent pod, so the garbage collector
removes it once the pod object is deleted, normally within seconds. If the
agent pod object stays behind, in `Error` say, the workload and its pods stay
until the agent Job's TTL removes the pod, about ten minutes.

**The registry and the code are read once, at start.** A deploy that changes
`launch.py`, a timeout or `spot_queues` reaches only steps that start after it;
a running launcher keeps what it started with.

The outcomes the timing record carries:

| Outcome | Meaning |
| --- | --- |
| `succeeded`, `failed` | The workload finished. `failed` carries the pods' exit code. |
| `cancelled` | Buildkite cancelled the step. |
| `not_admitted` | Not admitted, or not dispatched, within its budget; or a resubmission failed. |
| `evicted_storage` | A pod was evicted for its disk use. |
| `unfit` | A pod asks for more than any node of its shape has. |
| `removed` | The Job or JobSet disappeared from under the launcher. |
| `api_unreachable` | The manager's API server did not answer for 10 minutes. |
| `error` | The launcher itself failed; the traceback is in the step log. |

The dashboard counts every outcome other than `succeeded`, `failed` and
`cancelled` as an infrastructure failure.

## Runbook

### Before adding a region

Terraform here owns clusters, not networking. A region needs a Cloud Router and
a Cloud NAT before a private cluster in it can pull from registry.k8s.io, and
neither is declared in this config: a NAT gateway covers every subnet range in
its region and network, so it is shared by everything there rather than owned by
one cluster, and a second gateway over ranges another already claims is refused
at apply.

They are named for the network and the region they serve — `default-us-central1-router`,
`default-us-central1-nat` — and not for this fleet, which merely happens to be
their first tenant.

```bash
gcloud compute routers create default-<region>-router \
  --project cloud-ullm-inference-ci-cd --region <region> --network default
gcloud compute routers nats create default-<region>-nat \
  --project cloud-ullm-inference-ci-cd --region <region> --router default-<region>-router \
  --auto-allocate-nat-external-ips --nat-all-subnet-ip-ranges
```

Check before creating: one may already be there for another tenant.

```bash
gcloud compute routers list --project cloud-ullm-inference-ci-cd
```

### Add a TPU shape

Add it to `tpu_node_pools` in the worker's module block, then `terraform apply`
in its environment, `generate_manifests.py`, `deploy_manifests.py`. That creates
the node pool, a ResourceFlavor, a ClusterQueue and a LocalQueue, and puts the
shape in the profile registry the launcher matches against. Steps reach it by
`--machine-type` / `--topology`; there is no new Buildkite queue.

A machine type and a topology together identify a shape, and both are needed: a
2x4 slice of v6e is eight chips either as one `ct6e-standard-8t` or as two
`ct6e-standard-4t`, and which it is decides the host, the pod count and the
quota.

`min_nodes` and `max_nodes` are in **nodes**, as on a GKE node pool;
`nominal_quota` is in **chips**, as the ClusterQueue's `nominalQuota`. Summed
across a cluster, `nominal_quota` should come to the chips the reservation
actually has free; `max_nodes` deliberately oversubscribes so a shape can
borrow, and `min_nodes` is the only one that really partitions the
reservation, since those chips stay with one shape once booted.

A single-host entry sizes its pool with `min_nodes` and `max_nodes`. A
multi-host entry takes `slices` instead: GKE sizes each multi-host pool at
exactly one slice and rejects any other size, so `slices` is how many slice
pools the shape gets.

### Add a worker

Workers are grouped into environments, `workers/<environment>/`: one Terraform
root and one state each, applied by whoever administers it. Ours is
`workers/cloud-ullm-inference-ci-cd/`, with the v6e and v7x workers; a partner
gets an environment of their own, for one worker or several. Each worker is a
module block calling `modules/worker`, in a file named for the worker. The
manager lists the workers it dispatches to, and nothing else connects them, so
nobody needs a role in a project that is not theirs.

That works because nothing reaches a worker through its project. Every worker
joins the manager's fleet. Kueue, the launcher and the deploy reach it through
Connect Gateway, which checks IAM on the manager's project and then the
worker's own RBAC. Its pods authenticate in its own project's Workload Identity
pool, which the manager grants by name.

What the environment's administrators need, run and do not provide is in
[`modules/worker/README.md`](modules/worker/README.md); for a partner, that is
the page to send. Our side:

1. **Outside `cloud-ullm-inference-ci-cd`, let them create the membership.**
   Add whoever will run their apply to `worker_deployers` in
   `manager/prod.auto.tfvars` and apply `manager/`. GKE creates the cluster's
   fleet membership in the manager's project as that identity. Remove them
   once the cluster exists.
2. **Write the module block,** on a branch. In an existing environment, add
   `<name>.tf` beside the others. For a new one, copy `main.tf` and one
   worker's file from `workers/cloud-ullm-inference-ci-cd/` into
   `workers/<environment>/` and change the backend and locals. Then set the
   module's arguments: name, project, region, network, a `/28` for the control
   plane, and the pools and quotas. `tpu-ci-<name>` is the cluster's fleet
   membership ID, so `<name>` is unique in the fleet. Leave `cpu_workloads`
   unset unless the fleet's CPU-only steps should run there too.
3. **They apply it.** The apply also binds the fleet's deployer account to
   `cluster-admin` in the new cluster, which is how the deploy gets in. In our
   own projects we apply it ourselves, and the account's IAM lets it in.
4. **List it, apply the manager, generate, deploy.** Add
   `<name> = { project, location }` to `workers` in `manager/prod.auto.tfvars`
   and apply `manager/`. That grants the worker's node account Artifact
   Registry, and its project's pool the CI buckets, BigQuery and the fleet
   secrets. It comes after step 3 because the grants name the node account the
   worker's module creates. Then `generate_manifests.py`, which adds the worker
   to the manager's queues for its shapes with its quota added to the fleet's,
   and `deploy_manifests.py`.

After that, a pool or quota change is an edit to the worker's module block,
which its environment's administrators apply and we generate and deploy. To
take a worker out again, see "Pause or remove a worker".

Whoever administers a worker can read everything on it, including the secrets
in `env_secrets`, which are synced to every worker. Decide that before listing
a partner's worker; the agent token and the git deploy key stay on the manager
either way.

### Pause or remove a worker

**Pause** stops the manager dispatching to a worker and leaves everything else
in place, so it is undone the same way:

1. Set `dispatch = false` on its entry in `workers` in
   `manager/prod.auto.tfvars`.
2. `generate_manifests.py` and `deploy_manifests.py`. The manager's queues lose
   the worker's quota and it leaves every MultiKueueConfig; its own manifests
   and its MultiKueueCluster are still rendered and deployed, and no
   Terraform changes.

Resume by removing the line, then generate and deploy. Pausing the only worker
with some shape is refused: that shape would leave the tree, and the deploy
keeps a queue that leaves the tree, so the manager would go on dispatching to
the paused worker.

Do it while the worker is idle (`kubectl get workloads -n buildkite` on the
manager, or the dashboard). What Kueue does with a workload already running on
a worker that leaves its MultiKueueConfig has not been tested here; expect it
to be requeued and rerun elsewhere.

**Remove** takes it out of the fleet, in this order:

1. Pause it, as above, and wait for it to be idle.
2. Take its entry out of `workers`, apply `manager/` (its grants go), then
   generate and deploy. The deploy deletes its MultiKueueCluster from the
   manager, so the manager matches git again.
3. Its environment's administrators destroy it: empty its two cache buckets
   (see "Tear the fleet down"), then `terraform -chdir=workers/<env> destroy`,
   or remove just its module block and apply if the environment has others.
4. Delete its module block, or the environment, from git - after step 3, since
   that is the configuration the destroy needs.

Reverting the PR that listed a worker is steps 2 to 4 without the pause.

### Rotate the Buildkite agent token

Add a new version to `vllm_buildkite_agent_token` in Secret Manager, then:

```bash
kubectl rollout restart deploy/agent-stack-k8s -n buildkite
```

SecretSync polls rather than watches, so the Kubernetes Secret catches up within
about five minutes. **The restart is not optional**: the controller reads the
token once at startup and holds it for the life of the process, so without it
the fleet keeps using the old token until something else restarts the pod.

This is the same secret the bare-metal agents register with. Rotating it affects
both lanes.

### Rebuild the launcher image

Manual, by design — it changes only when the Cloud CLI version does.

```bash
cd kueue/launcher
gcloud builds submit --project cloud-ullm-inference-ci-cd --region us-central1 \
  --config cloudbuild.yaml \
  --gcs-source-staging-dir gs://cloud-ullm-inference-ci-cd-tf-state/cloudbuild-source \
  --substitutions _CLOUD_SDK_VERSION=584.0.0,_REVISION=1 .
```

Then point `launcher_image` in `manager/prod.auto.tfvars` at the new tag and
redeploy.
Bump `_REVISION` when the Dockerfile changes without the base image changing, so
a tag always names one set of bytes. The staging directory has to be named: the
default is a multi-region `us` bucket, which `constraints/gcp.resourceLocations`
refuses in this org.

### Rebuild the dashboard

After any change under `dashboard/`, bump `_REVISION`:

```bash
cd dashboard
gcloud builds submit --project cloud-ullm-inference-ci-cd --region us-central1 \
  --config cloudbuild.yaml \
  --gcs-source-staging-dir gs://cloud-ullm-inference-ci-cd-tf-state/cloudbuild-source \
  --substitutions _REVISION=2 .
```

Then set `dashboard_image` in `manager/prod.auto.tfvars` to the new tag and
`terraform -chdir=manager apply`. To try a change first, run it against the live fleet with
your own credentials - gcloud for Google APIs, the `bk` CLI for Buildkite - with
the environment `dashboard.tf` sets:

```bash
PROJECT_ID=... GATEWAY_URL=... CLUSTERS='[{"name": ..., "gateway": ...}, ...]' \
  KUEUE_METRICS_CLUSTER=tpu-ci-manager BUILDKITE_ORG=vllm BUILDKITE_CLUSTER_ID=... \
  TIMING_TABLE=cloud-ullm-inference-ci-cd.ci_efficiency_metrics.kube_workload_timing \
  ./dashboard/app.py --local --port 8080
```

### Tear the fleet down

**Empty the cache buckets first.** They are `force_destroy = false`, so a
`terraform destroy` will fail on them rather than delete them — which is the
intent. Rebuilding the caches from cold costs roughly 2.7x a suite's chips, so
emptying them is a deliberate step and not something a destroy does on the way
past.

There are two per worker cluster — a compilation cache and a model cache — and
their names carry a hash of the cluster's project and region, so list them
rather than typing them. The workers' environments first, then the manager:

```bash
terraform -chdir=workers/<env> state list | grep google_storage_bucket.workload
gcloud storage rm -r 'gs://tpu-ci-cache-<hash>/**' 'gs://tpu-ci-models-<hash>/**'
terraform -chdir=workers/<env> destroy
terraform -chdir=manager destroy
```

## Things that will surprise you

**A deploy briefly rejects pod creation, cluster-wide.** Kueue's pod webhooks
are `failurePolicy: Fail` and scoped to every namespace but `kube-system` and
`kueue-system`, with no object selector. While `kueue-controller-manager` rolls,
pod creation anywhere in the cluster fails. It is seconds, and it retries, but
do not deploy into the middle of something that cannot tolerate it. JobSet's
webhooks are also `Fail`, though those are narrowed to pods that already carry a
JobSet label.

**No cluster in this fleet may be Autopilot.** Autopilot writes a nodeAffinity
on `cloud.google.com/extended-duration-pods` into any podspec that arrives
without one. MultiKueue copies the podspec to the worker unchanged, no Standard
node carries that label, and the pod is then unschedulable with nothing
reporting an error: the step simply waits out its timeout. The manager is where
every workload podspec is born, so that is the one it would break.

**A very short workload can lose its output.** MultiKueue deletes the remote Job
when it completes and the pods go with it, so a workload that lives a few
seconds can be created and removed between two log polls. The launcher polls
faster before the first line arrives, which covers the built-in Job. The step
still passes or fails correctly and says when output is missing; the container
output is in Cloud Logging either way.

**The Kueue and JobSet controllers run two replicas at kube-dns's priority.**
Upstream ships one replica with no priority class, and GKE scales kube-dns with
the cluster: every TPU node that joins can add a kube-dns pod, which lands on a
system node and preempts whatever has a lower priority. That was the
controllers, several times a day, and each time their webhooks had no endpoint
until the pod came back. `controllers/` gives each a second replica (leader
election picks one to reconcile; both serve the webhook), `system-cluster-critical`
with the ResourceQuota GKE requires for it outside `kube-system`, a preference
for separate nodes, and a disruption budget. It is applied under its own field
manager, because the manager's Kueue Deployment already carries the auth-plugin
overlay, and the deploy leaves upstream's `replicas: 1` out of the release so
the two applies do not trade the replica count back and forth.

The Buildkite controller gets the same class (`chart-overlay/`, applied after
the chart) but stays at one replica: it has no leader election, and two
controllers sharing an ID can each reserve the same job. Its pod is marked
`safe-to-evict: "false"`, because the cluster autoscaler emptying its node was
what restarted it, and job pickup stops until it is back. Running jobs do not
depend on it.

**Every v7x shape has quota of its own, and 2x2x4 takes it back.** The 128
chips split 72 to `tpu7x-standard-4t-2x2x1` (18 slices), 32 to
`tpu7x-standard-4t-2x2x4` (two slices), 16 to `tpu7x-standard-4t-2x2x2` (two
slices) and 8 to `tpu7x-standard-1t-1x1x1`; any shape can borrow what the
others leave idle.
Kueue considers a workload that fits a queue's own quota before one that has to
borrow, so a shape under its nominal gets freed chips first. 2x2x4 also evicts
lower-priority borrowers to get its slices back (`reclaimWithinCohort:
LowerPriority`); the other shapes, 2x2x2 included, wait for borrowers to finish
rather than throw their runs away. A reclaimed slice still waits for the
evicted nodes to scale down, about ten minutes, before its own nodes boot. If a shape is starving, the lever is the
split in `workers/us-central1/main.tf`, not the node pools.

**8-chip 2x2x1 steps can wait behind 4-chip ones.** The 2x2x1 queue mixes
4-chip steps with 8-chip ones that take two VMs. Every queue is
`BestEffortFIFO`, which passes over a workload that does not fit for a smaller
one behind it, so freed chips go four at a time to 4-chip steps while an 8-chip
step waits. `StrictFIFO` (`queueing_strategy` on the shape's pool) would admit
in order, but in Kueue v0.20.1 a strict head that has to borrow holds its chips
on every scheduling pass, and 2x2x2 and 1x1x1 admit nothing while it does.

**A cold pool's first image pull is slow, and that is not streaming failing.**
Image streaming is on for every TPU pool, but GKE serves an image it has
converted and it converts each digest once. CI pushes a new digest every build,
so the first node to want it waits out the conversion — measured at 87s for a
2.7 GB image — and every node after it mounts the same digest in about two
seconds. Caching layers on the node cannot help; the digest is new every build.

**A step may legitimately queue for hours, but not forever inside the
launcher.** `tpu_queue_max_seconds` and `tpu_runtime_max_seconds` are separate
budgets — how long to wait for chips, and how long to hold them. The agent
pod's own deadline (`job-active-deadline-seconds` in the agent-stack config) is
derived to sit above both plus two hours, so one of the launcher's budgets
always fires first and the step log says which. A kube step that ends as
`exit_status -1` is therefore a cancellation or the workload's own deadline, not
a Buildkite cap. Queueing that should not hold an agent pod at all belongs in
Buildkite: a step held by a `concurrency_group` is `limited`, has a null
`started_at`, and burns no clock. The launcher annotates what it is waiting for;
read that before assuming a fault.

**A reservation that never reaches a worker is resubmitted, not waited out.**
MultiKueue sometimes reconciles a fresh reservation once, creates no copy on
any worker and never comes back to it: the chips are reserved, nothing runs,
and the step shows `waiting for admission` until `tpu_admission_max_seconds`.
It can also name a worker, have the worker admit the copy, and then fail to
record that on the manager: the webhook rejects `clusterName` because the
nomination it must match is already gone, so the step shows `dispatching to
...` followed by `waiting for admission` and the manager's controller log
repeats `must be one of the nominatedClusterNames`. In either state the
launcher deletes and recreates the workload after `tpu_dispatch_retry_seconds`
with no worker named, up to `tpu_dispatch_retries` times, and logs
`resubmitting`. In the second it first waits for that worker to drop the old
copy's pods, since MultiKueue would otherwise adopt the old remote Job for the
new object. A workload waiting on a worker for a slice to be rebuilt keeps that
worker named and is left alone. The timing table counts resubmissions in
`redispatches`.

**A pod evicted for its disk use fails the step.** The pod failure policy
ignores `DisruptionTarget` so a lost node reruns the pod rather than failing the
run, and a kubelet eviction carries that condition too. For an eviction over
ephemeral storage or an `emptyDir` limit the rerun writes the same files and is
evicted again, until the step's deadline. The launcher fails the step on the
first such eviction, prints the kubelet's message, which names the container and
how much it used, and records the outcome `evicted_storage`.

**A pod no node of its shape can hold fails the step.** Kueue admits on chips
alone, so a pod that asks for more ephemeral storage, memory or CPU than a node
of its shape has is admitted and then never placed, holding its chips' quota
until `waitForPodsReady` gives up hours later. While an admitted pod has no
node, the launcher reads the cluster autoscaler's `NotTriggerScaleUp` events
for it; `N Insufficient ephemeral-storage` (or memory, cpu) there is judged
against the pools' node templates, so it means no new node would fit either,
where a busy fleet reads `max node group size reached`. The launcher posts a
warning on the build page naming the request, and if the autoscaler still says
so 10 minutes later it fails the step and records the outcome `unfit`.

**us-central1 holds both the manager and a worker.** They are separate clusters
with separate control-plane CIDRs, but they share the region's Cloud Router and
Cloud NAT, and both pull from the same Artifact Registry. Do not declare a second
NAT gateway for the worker.

**Not every controller setting is in our values.** The effective config is:

```bash
kubectl get cm agent-stack-k8s-config -n buildkite -o jsonpath='{.data.config\.yaml}'
```

`kueue/templates/agent_stack_values.yaml.tpl` sets only the queue and the Secret
name; everything else in that output is a chart or controller default that we
accept, including `job-ttl` and `max-in-flight`. Read it there rather than
guessing — and note the chart pastes our block under keys of its own, so
anything it derives must be left out of the template or the controller's decoder
rejects the duplicate.

## Spot overflow

A single-host shape can have Spot capacity beside its reservation:
`spot = { max_nodes = N }` on its `tpu_node_pools` entry adds a `<shape>-spot`
node pool in the shape's zone, or in each of `spot.zones`, with no reservation
and a `cloud.google.com/gke-spot` taint, and gives the shape's ClusterQueue a
second flavor, `<generation>-spot`. Nothing about a step changes: no flag, no queue,
no label. Kueue tries the reserved flavor first, borrowing from the cohort
included, and admits onto Spot only when the reservation cannot take the
workload - overflow, since the reservation is paid for either way. It reaches
for Spot rather than preempt a running workload to reclaim lent quota. A
shape's Spot quota is the chips its own Spot pool can hold, with
`borrowingLimit: 0`: reserved quota can be borrowed because every shape's pool
draws on one reservation, but another shape's Spot pool has nodes of the wrong
shape.

The Spot flavor is the only thing that names Spot nodes: Kueue adds its
`cloud.google.com/gke-spot=true` nodeSelector and its toleration to a pod it
admits on that flavor and to no other, so a workload admitted on the
reservation can never land on a Spot node. The reserved flavor of a generation
with Spot names `cloud.google.com/gke-provisioning: standard`, which GKE puts on
every reserved node (a Spot node says `spot`) and in each pool's node template,
so a pool at zero still scales up for the nodeSelector Kueue writes from it.

Which steps reach Spot is the launcher's choice, by priority and by wait. A
step above `tpu_spot_max_priority` (50: post-merge, oncall-fix) never uses
Spot; it waits for reserved chips and, outranking the rest, takes them first as
they free up. A step at or below it (pre-merge, integration, unclassed, low)
is submitted with Spot ruled out too, and only once it has waited
`tpu_spot_after_seconds` (10 minutes) for reserved chips is it resubmitted
pinned to Spot - the step log says `waited Ns for reserved chips - resubmitting
to run on Spot`.

Every submission names exactly one flavor, because under MultiKueue the manager
and the worker each choose one: allowed both, a worker with reserved room put
workloads on reserved that the manager had booked on Spot, and the two sides'
accounting parted - by 42 chips on 10-13, enough that a post-merge JobSet the
manager admitted could not fit on the worker, which does not preempt. Kueue
matches a pod's node affinity against a flavor using only the keys that flavor
defines (`flavorassigner.go`), so the pins are `gke-spot NotIn ["true"]` for
reserved (only the Spot flavor defines `gke-spot`) and `gke-spot In ["true"]`
plus `gke-provisioning In ["spot"]` for Spot (only the reserved flavor defines
`gke-provisioning`). A rule on `gke-spot` alone cannot rule the reserved flavor
out: #795 tried, and stranded pods admitted on reserved with a Spot-only
affinity and no Spot toleration (#796). Kueue sends a workload to Spot the moment the reservation
is full, so without the wait a small overflow that the reservation would clear
in minutes landed on Spot, held Spot quota through a stockout, and left
reserved chips idle as they freed up: on 10-13, 27 chip-hours idle while steps
waited in under two hours, against 32 chip-hours Spot delivered.

Spot quota is a count, not capacity. Kueue admits onto it whether or not GCE
has a Spot VM to give, and a pool that cannot scale up changes nothing Kueue
sees. The launcher covers that: when a chip-holding pod admitted onto Spot (it
carries the flavor's nodeSelector) is still unscheduled after
`tpu_spot_wait_seconds`, it deletes the workload and submits it again, once,
with a required node affinity `cloud.google.com/gke-spot NotIn ["true"]` on its
chip-holding pods. Kueue skips a flavor whose nodeLabels that affinity rules
out, so the resubmission waits for the reservation as a shape without Spot
would. The step log says `admitted onto Spot but no Spot node`.

The cluster autoscaler's `FailedScaleUp` event on the pod ("GCE out of
resources") moves that sooner: the launcher reads the pending pod's events every
30 seconds and resubmits the same way `tpu_spot_failed_scale_up_seconds` (5
minutes) after the first one, if the pod still has no node. Not at once,
because the event goes to every pod a batch of new nodes was for when any node
in the batch fails, and the pool keeps retrying: on 10-13 all 9 Spot pods that
had one got a node, 6 of them within 5 minutes. The step log says `no Spot node
added for <pod> yet` when the event arrives.

A reclaimed Spot node takes its pod with it. The pod is marked
`DisruptionTarget`, which the pod failure policy ignores, and the Job would
replace it pinned to Spot like the first. Spot nodes go in waves - on 10-13 one
lasted 3 minutes at the median on 1x1x1 and 14 on 2x2x1, and 17 of the 41 steps
reclaimed once were reclaimed again - so the launcher sends the step back to the
queue instead, as at submission: resubmitted with Spot ruled out, it reruns from
the start on reserved chips, and is offered Spot again only after another
`tpu_spot_after_seconds` of waiting for them. The step log says `Spot node
reclaimed under <pod> - back to the queue`. A step that found no Spot node is
different: the stockout fallback keeps it off Spot for good.
`recoveryTimeout` (30 minutes, in `kueue/common-config.yaml`) remains behind
both, requeuing a workload whose replacement pod cannot start.

Multi-host shapes cannot have Spot: GKE reclaims a slice whole, and Kueue
admits chips rather than slices.

## The health dashboard

`terraform -chdir=manager output dashboard_url`, behind IAP;
`dashboard_viewers` in `manager/prod.auto.tfvars` says who gets in. It is the page Buildkite cannot give the
kube fleet, where every step is on the one `kube` queue until the launcher picks
a Kueue queue for it, and where a step shows as running from the moment its
agent pod starts.

**Overview** (`/`), from the same snapshot as Live. It opens with one line
saying whether anything is wrong and how full each generation is, then only
the checks that warn or fail, then where the steps are - in Buildkite, an agent
pod starting, waiting for quota, handing to a worker, waiting for a node (how
many on Spot), running - with the oldest at each and a stage marked once its
oldest is past its limit, then a row per queue: running, waiting for a node,
waiting for quota, chips held (own, borrowed, and Spot running and waiting for
a node) and nodes up. Below that, a diagram of the
path a step takes - Buildkite, the manager's agent pods, Kueue and MultiKueue,
then one box per worker with its node pools - with how many steps are at each
stage now. Dots move along a path while steps are past it. Each box's mark is
the worst of the Health checks that belong to it, and clicking it opens that
component's details below the diagram. The glossary and what each data
source reads, and what to check when one fails, live here. Only what the
dashboard reads live is drawn, so it cannot go stale the way a diagram in a
doc does.

**Live** (`/live`), reloaded every minute - but not while a section, a build's steps or the rows behind Show more are open, since a reload would close them:

- **Health** - one check per stage from Buildkite to a TPU pod: the Buildkite
  controller polling and creating, Kueue up on every cluster, workers
  connected, dispatches not stalled, the oldest pending workload against
  `tpu_queue_max_seconds`, admitted workloads near `tpu_test_max_seconds`,
  infrastructure failures and evictions in the last 24 hours, fleet warnings
  in the last hour, agent pods the manager cannot schedule, and steps no agent
  picked up.
- **Kube jobs** - where every Buildkite kube job is, grouped by build:
  concurrency held, waiting for an agent pod, agent pod pending, launching,
  pending in Kueue, dispatching, pods starting, running. Joined from the
  job's Buildkite state, its agent pod and its Kueue workload, since Buildkite
  alone shows a kube job as running once its agent pod starts.
- **Quota now** - per cohort, chips in use against nominal, free, pending and
  busy; per queue, its nominal, usage, what it borrows or leaves idle, and
  whether it evicts borrowers.
- **Per queue** - what is running, by build, with the chips it holds; the
  first five pending in the order Kueue will consider them, the rest a click away, with their priority
  class, from Kueue's visibility API - the check that priority orders the
  queue - and the rest counted by build; then Kueue's reason for anything
  pending. Only the head of a queue carries a reason; the rest are counted as
  queued behind it.
- **Cluster problems** - events from every cluster's `buildkite` namespace
  that are the fleet's fault (failed creates, failed scale-ups, mount
  failures, evictions, OOM kills), repeats grouped. Routine warnings - Kueue's
  backlog, scheduling while a pool scales, agent teardown, test failures - are
  collapsed beneath.

**Capacity** (`/capacity`; `/trends` and `/history` redirect here), for the last 6 hours, 24 hours, 7 days or 30 days (`/api/history` also takes `?start=&end=` in epoch seconds, up to 90 days):
is capacity right, and where does chip-time go. At the top, PR build wall
clock over the range: for the pipelines with at least one clean kube PR build a
stretch, the median and p90 of creation to the last step finishing, by when the
build was created (`fleet.fetch_steps`, the builds Pipelines → Steps reads). Then
a card per generation:
tiles for the reserved chips and the peak asked of them, the share of the
reservation used and busy computing, the queue wait, and what Spot delivered;
the queue wait of every shape in the cohort together over time; supply and demand - each shape's admitted chips stacked under the nominal, each
Spot shape's chips running on Spot nodes above them, and hatched, the chips of
steps still waiting for quota, so demand the reservation could not meet shows
above the line; where the reserved chips went - busy computing, held but not
computing, admitted before a node was up, idle on a node, or quota unused; and
chips admitted with no node to run on, a scale-up on the reservation or a
stockout on Spot. Then a table of shapes and, open below it, a block per shape
with its rules, waits, cold start and evictions, its chips admitted against on
nodes with its Spot stacked on top, and its queue wait over time
(`fleet.fetch_wait_series`, the series Migration draws). Spot's quota appears
nowhere: it is a count GCE has never come near filling. Each chart point is the
step's average, taken over the fleet-wide value at each minute - collapsed to
the newest Kueue controller pod and summed over nodes first, so a controller
roll or a node that came and went does not inflate it. Node history comes from
GKE's per-node metrics, which name a node and nothing else; the GKE API's list
of node pools ties each name to its pool through the pool's instance group,
which outlives the nodes it creates. TensorCore busy is read per node too, so
a Spot node's compute is not counted as the reservation's.

**Review** (`/review`, the last day, week or 30 days to the last whole hour,
against the same length before it) is the look back for a weekly review: per
generation, chips held, busy, idle while jobs waited, waits, startup, failures,
requeues and Spot, with the change; day by day where the period is a week or
more; then infrastructure failures, PR build wall clock, and the fleet's merged
changes. Infrastructure failures are of two kinds. Kube steps the launcher ended
for the fleet's reasons - its outcome in `kube_workload_timing` is not
succeeded, failed or cancelled - each listed with what the outcome means, its
Buildkite job and its launcher log in Logs Explorer. And failed jobs on the CPU
queues (`fleet.CPU_QUEUES`: image builds, gates and notifications), from
`step_execution_logs`: every one exits 1, so whether the fleet failed it is only
in its log. `app.LogCauses` reads each failed job's log once through the
Buildkite API, two at a time in the background, newest first, and keeps the
result, since a finished job's log does not change; `fleet.classify_log` matches
the last 400 KB against `fleet.LOG_CAUSES` - disk full, killed for memory, the
Docker daemon, the image registry, a package download, the network - or, with
nothing matching, an agent lost (exit -1) or a timeout. The rest count as the
job's own. Each of the fleet's is listed with the log line that gave its cause
and a link to the job. A fresh instance reads the period's logs again; the page
says how many are still being read.

**Migration** (`/migration`, last 24 hours by default; `/compare` and `/baseline`
still work) puts the bare-metal fleet
before the migration beside the kube fleet over the window: per generation,
utilization, chips, work a day and kube's share of the work - what the
bare-metal queues still run, from `step_execution_logs` - and per shape, steps
a day and waits on both sides. The bare-metal side is static:
`dashboard/baseline/build_baseline.py` builds `premigration-2026-09.json` from
the pre-migration snapshot (2026-09-05 to 09-28). When bare metal's share
reaches zero the comparison is like for like with no change to the page.

Its headline is *idle while jobs waited*: minute by minute, the idle chips that
whole waiting jobs would have fit in, smallest first. On bare metal a job ran
only on its own shape's VMs, so one shape idled while another queued; the
baseline builder works it out from Buildkite's per-minute counts of connected
and busy agents and of jobs ready with no agent. On kube it comes from Kueue's
admitted, nominal and pending series, read a minute apart rather than at
Capacity's step (`fleet.fetch_waits`), since an hour that queued in its first half
and idled in its second would otherwise count as both at once. This and chips
held are worked out a day at a time and shown as the mean, median and worst day;
kube's days are the 24-hour stretches back from the window's end, so its median
and worst need the 7- or 30-day window.

**Jobs** (`/jobs`, last 24 hours by default) is the per-queue job list Buildkite
had for each bare-metal queue and cannot give now that every kube step runs on
its one `kube` queue: what is in flight on each Kueue queue, from the live
snapshot, then every workload that ended in the range from
`kube_workload_timing` (`fleet.fetch_jobs`), newest first, with its outcome,
exit code, wait, startup and run - a build a row, its steps opening beneath it
with the failures first, or job by job. Filters for queues (any number), outcome,
branch and pipeline and a search over step, build and branch sit above it, and a
line under them sums up what they leave. Each queue card on Live and each shape block on Capacity links to its queue's jobs. Both Jobs (`/jobs`) and Steps (`/steps`) count GitHub merge-queue (`gh-readonly-queue/*`) builds under `main` rather than `PRs`, keeping `main` metrics meaningful once post-merge push builds are skipped and keeping merge-queue runs out of PR wall-clock charts.

Migration also counts *infrastructure failures*: failed jobs whose log ends in
the fleet's fault - a full disk, a TPU that would not start, a bus error, a kill
for memory, an agent that died, a missing CI image - a day, by cause, bare
metal before beside kube. The cause is only in the log, so
`dashboard/baseline/failures.py` fetches each failed TPU job's log with `bk
api`, sorts it, and writes `failures.json`; the kube side is a snapshot of its
window, refreshed by rerunning the script (`TIMING_TABLE` set for `bq`).

Each health check on Live opens to **History & logs**: its status over the
last day, its latest changes, and the warnings and errors in the logs of the
component it watches - the Buildkite controller's, Kueue's, Kubernetes warning
events - with alike lines counted as one, and links to the same in Logs
Explorer. The history is the dashboard's own: it evaluates the checks every
half minute whether or not anyone is looking, and writes each change of status
as a structured line (`jsonPayload.health_check`) that Cloud Run keeps in
Cloud Logging. It reads logs through the `_Default` bucket's `_AllLogs` view
(`roles/logging.viewAccessor` there, in `dashboard.tf`).

Every page's terms have a tooltip, drawn from the same table as the glossary.

It reads every cluster through Connect Gateway as `tpu-ci-dashboard@`: the
manager with `kueue/templates/dashboard_rbac.yaml.tpl`, the workers with
`dashboard_rbac_worker.yaml.tpl`, events only. Both are applied by
`deploy_manifests.py` like the rest, so the page reports those reads as failed
until they are. A source that fails is named at the top of the page, and the
rest still render.

## Reading the metrics

Managed Prometheus scrapes Kueue on all three clusters and the Buildkite
controller on the manager. There is no Grafana and no Prometheus server to point
a browser at; query Cloud Monitoring's Prometheus-compatible endpoint:

```bash
curl -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  --data-urlencode 'query=kueue_cluster_queue_resource_usage' \
  https://monitoring.googleapis.com/v1/projects/cloud-ullm-inference-ci-cd/location/global/prometheus/api/v1/query
```

Utilisation is `kueue_cluster_queue_resource_usage` over
`kueue_cluster_queue_nominal_quota`, both labelled by flavor and resource. Queue
time is `kueue_admission_wait_time_seconds`, which is the number Buildkite
cannot give you: its `started_at` includes the wait. Whether the fleet is being
fed at all is `buildkite_monitor_monitor_up` and
`buildkite_scheduler_job_create_success_total` against `job_create_calls_total`.

Read utilisation on the manager. It is the only cluster holding every
ClusterQueue, because it admits against fleet quota; a worker reports only its
own generation, and what a worker's numbers tell you is whether something
admitted here is also through admission there.

**Collapse `instance` before you sum.** A rolled controller leaves the old pod's
series inside the five-minute lookback, so both report and a plain
`sum(kueue_cluster_queue_resource_usage{flavor="tpu7x"})` reads sixteen chips on
an eight-chip fleet. Aggregate it away first:

```
sum(max by (cluster_queue,flavor,resource) (kueue_cluster_queue_resource_usage{flavor="tpu7x"}))
```

A usage-over-quota ratio hides this, because both sides double and the ratio
comes out right for the wrong reason. Do not take a plausible ratio as evidence
the query is sound.

## Debugging a step

Start from the step's log in Buildkite. The launcher's own lines start with
`~~~ launcher:`; it also annotates the build while a step waits for quota or
cannot be placed. The dashboard's Live page shows where every kube job is now,
and Jobs lists the ones that ended, with their outcome.

A kube step is `running` in Buildkite from the moment its agent pod starts,
which can be hours before it gets chips. Whether it has them is the launcher's
`admitted to worker cluster` line, or `admitted_at` in the timing table.

### What the launcher's status lines mean

| Line | Meaning |
| --- | --- |
| `waiting for quota: ...` | In Kueue's queue. Kueue's message says how many chips are missing. Normal. |
| `waiting for admission`, `dispatching to <worker>` | Quota is reserved and MultiKueue is placing it on a worker, which takes seconds. Longer: "A reservation that never reaches a worker". |
| `quota reserved Ns ago but ... - resubmitting` | MultiKueue dropped the dispatch and the launcher resubmitted. |
| `admitted to worker cluster <worker>` | Admitted on the worker. The pods come next. |
| `pod not scheduled yet: Unschedulable` | Waiting for a node, usually a scale-up. A multi-host slice from zero takes several minutes. Read the events below. |
| `container waiting: PodInitializing` | The node is up. Init containers and the gcsfuse sidecar are starting, or the image is pulling. |
| `evicted (...), waiting for re-admission` | Kueue preempted it (a reclaim within the cohort) and it is back in the queue. |
| `waited Ns for reserved chips - resubmitting to run on Spot`, `admitted onto Spot but no Spot node`, `Spot node reclaimed under <pod> - back to the queue` | Spot overflow; see "Spot overflow". |
| `no <queue> node can hold <pod>` | A pod asks for more ephemeral storage, memory or CPU than a node of its shape has. It fails after 10 minutes unless the autoscaler changes its mind. |
| `pod evicted for its disk use` | Something in the workload wrote to the container's disk; the message names the container and the amount. |
| `not admitted within Ns`, `not dispatched within Ns` | A budget ran out. Look at the queue's backlog before at the step. |
| `no answer from the manager's API server` | The manager's control plane was unreachable for 10 minutes. The workload may need deleting by hand. |

### From a Buildkite job to its objects

Everything is named from the Buildkite job UUID:

| Object | Cluster | Name |
| --- | --- | --- |
| Agent pod, running the launcher | manager | `buildkite-<job uuid>-<suffix>`, container `launcher` |
| Job or JobSet | manager, copied to a worker | `bk-<job uuid without dashes>` |
| Kueue Workload | manager and worker | `job-bk-<...>-<hash>` or `jobset-bk-<...>-<hash>` |
| Workload pods | worker | `bk-<...>-<suffix>` for a Job, `bk-<...>-<role>-<job>-<pod>-<suffix>` for a JobSet; container `workload` |

The workload's pods carry `buildkite.com/job-id=<job uuid>`:

```bash
kubectl -n buildkite get pods -l buildkite.com/job-id=<job uuid>    # on the worker
```

### Reading logs and events after the pods are gone

Cloud Logging keeps every cluster's container output and Kubernetes events:

```bash
P=cloud-ullm-inference-ci-cd

# The launcher's log.
gcloud logging read --project $P --freshness 1d --order asc --format='value(timestamp,textPayload)' \
  'resource.type="k8s_container" AND resource.labels.container_name="launcher" AND resource.labels.pod_name:"buildkite-<job uuid>"'

# The workload's own output, on the worker.
gcloud logging read --project $P --freshness 1d --order asc --format='value(timestamp,resource.labels.pod_name,textPayload)' \
  'resource.type="k8s_container" AND resource.labels.container_name="workload" AND resource.labels.pod_name:"bk-<uuid without dashes>"'

# Events for the step's objects, on every cluster.
gcloud logging read --project $P --freshness 1d --order asc \
  --format='value(timestamp,resource.labels.cluster_name,jsonPayload.reason,jsonPayload.message)' \
  "logName=\"projects/$P/logs/events\" AND jsonPayload.involvedObject.name:\"bk-<uuid without dashes>\""

# The autoscaler's reasons, per node pool, for not adding a node.
gcloud logging read --project $P --freshness 2h --format=json \
  "logName=\"projects/$P/logs/container.googleapis.com%2Fcluster-autoscaler-visibility\" AND resource.labels.cluster_name=\"tpu-ci-us-central1\" AND jsonPayload.noDecisionStatus.noScaleUp:*"
```

How to read the events while a pod waits for a node:

- `TriggeredScaleUp` — a node, or a whole slice, is being made. Wait.
- `NotTriggerScaleUp` with `max node group size reached` or `in backoff after
  failed scale-up` — the shape is busy, or a scale-up failed and is being
  retried. Wait.
- `NotTriggerScaleUp` with `N Insufficient ephemeral-storage` (or `memory`,
  `cpu`) — no node of the shape would fit the pod, now or later. The launcher
  fails these as `unfit`; fix the manifest's requests.
- `FailedScaleUp` with `GCE out of resources` — a stockout, almost always on
  Spot. The launcher moves the step off Spot.
- `FailedScheduling` judges only the nodes that exist, and reads the same while
  a slice is still being made. It is not evidence of a problem by itself.
- `FailedCreate ... follower pod node selector for topology domain not found` —
  JobSet's exclusive placement holding a multi-host Job's other pods until its
  first pod has a node pool. Normal.
- `Evicted` — read the message: it names the resource and the container.

### Failures that look like the fleet's but are not

These happen inside the workload and only, or mostly, on kube. The fix is in
the test repo or its manifest.

| Symptom | Cause | What to do |
| --- | --- | --- |
| libtpu `START_SESSION failed` or `Cannot write the request to vBAR before deadline`; other ranks stuck in `CreateTpuSystemState` | A new process group started while the chip was still resetting after the previous one exited; the node's `tpu-device-plugin` logs `vBAR register read returned 0xffffffff`. libtpu reaches the chip through the device plugin's vBAR socket on GKE and not on a TPU VM. Also seen on a multi-host slice reused within a minute of the previous JobSet. | Read the device plugin's log on the node around the start. Wait for the device or retry the launch; it is not a port race. |
| Intermittent `SocketServer: Connection closed recv() == 0` between processes on one host | libtpu's `TPU_PROCESS_PORT` overlaps another listener; with `SO_REUSEPORT` the kernel splits connections between them. | Move one of them; tpu-inference #3730 moved libtpu to 8476+rank. |
| Multi-host engines hang creating the PJRT client after `Accessing retired flag 'eventmanager2_as_default_eventmanager'` | Two independent engines in the JobSet's default group, so GKE's webhook sets `MEGASCALE_NUM_SLICES=2` on both and each waits for a slice that never joins. | Give each engine its own `replicatedJobs[].groupName`. |
| Non-head pods fail importing a `trust_remote_code` module (`transformers_modules... has no attribute ...`) | The workers on a host race to write the HF modules cache, which starts empty in every pod. | Warm the cache once per pod before starting Ray (vllm-torchtpu `warm_remote_code.py`). |
| Pod evicted for ephemeral storage; the step fails `evicted_storage` | The workload writes to the container's disk, usually `~/.cache/vllm` (`VLLM_CACHE_ROOT` unset) or `HF_HOME` outside `/cache/hf/hub`. | Point it at `/cache/jax/...` and list the variable in the test repo's `common.sh` `FORWARD`. |
| `403 ... storage.buckets.get` from `runai_streamer` | The workload identity can read objects but not bucket metadata, which runai reads first. | Grant `roles/storage.bucketViewer` on that bucket. |
| `401 ... User Access Token ... is expired` in every step that downloads from HF | The HF token expired. | Add a version to the secret named by `env_secrets.HF_TOKEN` in `manager/prod.auto.tfvars`; SecretSync copies it within minutes. Check that the new token can read the gated repos too. |
| The Job is `Failed` with no failed container and no OOM | The workload's deadline: `tpu_test_max_seconds` unless the manifest or step states one. | Set `activeDeadlineSeconds` in the manifest or `TPU_MAX_RUNTIME_SECONDS` on the step. |

When comparing a kube step with the same step on the VM fleet, compare each
job's `exit_status`, not the build's state (many VM steps are `soft_fail`), and
compare the test phase (the pytest window), not the step's duration, which on
kube includes queueing and startup.

### Working down from admission

Everything here is on the manager unless it says otherwise.

```bash
# Is it admitted, and if not, why?
kubectl get workloads -n buildkite
kubectl describe workload -n buildkite <name>

# Is there quota for the shape it asked for? Check borrowing too: a shape with
# nominalQuota 0 is admitted only out of its cohort's idle chips.
kubectl get clusterqueue

# Admitted but nothing running: it is on the worker that owns that shape -
# tpu-ci-us-east5 for ct6e, tpu-ci-us-central1 for tpu7x.
kubectl --context gke_cloud-ullm-inference-ci-cd_<region>_<worker> get pods -n buildkite
```

Admission only means the chips are reserved. The pod still has to be scheduled,
the node possibly created from zero, and the image pulled — tens of minutes on a
cold pool. The launcher reports where it is in that gap; a step sitting quietly
at "waiting for a node" is usually the autoscaler, not a fault.

A workload that names a Spot pin but sits with `NoMatchingFlavor` was offered
Spot by a launcher that started before its queue's Spot flavor was removed; see
"Deploying" in `AGENTS.md`. Cancel and retry the step.

**What a Buildkite job is actually asking for.** The agent page will not tell
you. Its `k8s:node=` tag is the manager node the *agent* pod landed on — a CPU
machine — and nothing there names a TPU. The request lives in the Kueue
workload, which you can reach because the agent pod is `buildkite-<job-uuid>-…`
and its workload is `job-bk-<uuid with the dashes removed>-…`:

```bash
kubectl get workloads -n buildkite -o json | python3 -c '
import json, sys
for w in json.load(sys.stdin)["items"]:
    uid = w["metadata"]["name"].split("-")[2]
    pod = w["spec"]["podSets"][0]
    chips = (pod["template"]["spec"]["containers"][0]
             .get("resources", {}).get("limits", {}).get("google.com/tpu", "?"))
    cond = {c["type"]: c["status"] for c in w["status"].get("conditions", [])}
    print(uid, w["spec"]["queueName"], chips, "x", pod.get("count", 1),
          "ADMITTED" if cond.get("Admitted") == "True" else "waiting")'
```

Read it as a histogram rather than a list. Fifteen rows waiting on
`tpu7x-standard-4t-2x2x1` is not fifteen problems; it is one nightly whose
multichip steps all became runnable at once, against a queue that holds two of
them.

A shape with no node pool is an error at submission that lists the shapes the
fleet does have, rather than a workload queued forever against quota that does
not exist.
