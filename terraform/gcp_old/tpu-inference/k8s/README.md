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
- **Worker** — `tpu-ci-us-east5`, Standard, `us-east5`. v6e, 26 chips free of a
  128-chip reservation, as `ct6e-standard-1t` (1x1) and `ct6e-standard-8t` (2x4).
- **Worker** — `tpu-ci-us-central1`, Standard, `us-central1`. v7x, 8 chips, as
  `tpu7x-standard-1t` (1x1x1), `tpu7x-standard-4t` (2x2x1) and `tpu7x-standard-4t`
  (2x2x2, multi-host across two VMs). Every v7x step in the fleet runs here.

A worker reports to the manager over Connect Gateway and runs no agent of its
own. MultiKueue joins them: the manager admits, the worker executes. An operator
talks to the manager for almost everything.

Each worker also carries a `worker-cpu` ComputeClass for roles that hold no
chips, and the fleet's secrets, put there by GKE SecretSync — a workload pod
runs on the worker, so a `secretKeyRef` has to resolve there rather than on the
manager where the launcher built the podspec.

## Layout

| Path | What it is |
| --- | --- |
| `*.tf` | The clusters, node pools, buckets, IAM. Terraform owns infrastructure only. |
| `prod.auto.tfvars` | The fleet. Cluster list, shapes, reservations, versions, timeouts. The single input to both Terraform and the generator. |
| `scripts/generate_manifests.py` | Renders `kueue/generated/` from `prod.auto.tfvars`. |
| `scripts/deploy_manifests.py` | Installs Kueue and JobSet, applies `kueue/generated/`. |
| `kueue/templates/` | The templates the generator renders. |
| `kueue/generated/` | The YAML that actually gets applied. Committed on purpose — see below. |
| `kueue/launcher/` | The program every TPU step runs, its Job, and its image build. |
| `kueue/launcher/pod_defaults.yaml` | What the fleet gives a workload's pods: caches, gcsfuse settings, eviction and retry policy. One definition, inherited by the built-in Job and by every manifest. |
| `dashboard.tf`, `dashboard/` | The fleet's health dashboard: a Cloud Run service, its program (`app.py` serves, `fleet.py` reads, `views.py` and `charts.py` render), templates, static files and image build. |

Terraform stops at the cluster; `deploy_manifests.py` starts there. The
Kubernetes and Helm providers need a reachable API server at plan time, which
would make creating a cluster and configuring it two runs with a hand-edited
variable in between.

`kueue/generated/` is committed so that reviewing a quota change means reading
the YAML that will be applied rather than inferring it from a template.
`deploy_manifests.py` refuses to run if the committed tree differs from a fresh
render, so it doubles as a drift detector - but only between the templates and
the committed tree. It applies, never prunes, so **deleting an object from the
tree does not delete it from the clusters**: a change that removes a queue, a
flavor or an admission check needs the matching `kubectl delete` by hand, on the
manager and on every worker that carried it.

## Deploying a change

```bash
pip install -r scripts/requirements.txt

terraform init && terraform apply          # if any *.tf or the shape list changed
./scripts/generate_manifests.py            # if prod.auto.tfvars or a template changed
./scripts/deploy_manifests.py              # diff, confirm, apply
```

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

Add it to `tpu_node_pools` for the right cluster in `prod.auto.tfvars`, then
`terraform apply`, `generate_manifests.py`, `deploy_manifests.py`. That creates
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

Then point `launcher_image` in `prod.auto.tfvars` at the new tag and redeploy.
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

Then set `dashboard_image` in `prod.auto.tfvars` to the new tag and
`terraform apply`. To try a change first, run it against the live fleet with
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
rather than typing them:

```bash
terraform state list | grep google_storage_bucket.workload
gcloud storage rm -r 'gs://tpu-ci-cache-*/**' 'gs://tpu-ci-models-*/**'
terraform destroy
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

**Every v7x shape has quota of its own, and the multi-host shapes take it
back.** The 128 chips split 64 to `tpu7x-standard-4t-2x2x1`, 32 to
`tpu7x-standard-4t-2x2x4` (two slices), 16 to `tpu7x-standard-4t-2x2x2` (two
slices) and 16 to `tpu7x-standard-1t-1x1x1`; any shape can borrow what the
others leave idle. Kueue considers a workload that fits a queue's own quota
before one that has to borrow, so a shape under its nominal gets freed chips
first. 2x2x2 also evicts any borrower to get its slices back
(`reclaimWithinCohort: Any`) and 2x2x4 evicts lower-priority ones
(`LowerPriority`); the single-host shapes wait for borrowers to finish. A
reclaimed slice still waits for the evicted nodes to scale down, about ten
minutes, before its own nodes boot. If a shape is starving, the lever is the
split in `prod.auto.tfvars`, not the node pools.

**A cold pool's first image pull is slow, and that is not streaming failing.**
Image streaming is on for every TPU pool, but GKE serves an image it has
converted and it converts each digest once. CI pushes a new digest every build,
so the first node to want it waits out the conversion — measured at 87s for a
2.7 GB image — and every node after it mounts the same digest in about two
seconds. Caching layers on the node cannot help; the digest is new every build.

**A step may legitimately queue for hours, but not inside the launcher.**
`tpu_queue_max_seconds` and `tpu_runtime_max_seconds` are separate budgets —
how long to wait for chips, and how long to hold them — and the first is set
well under what the fleet actually makes a step wait. Something ends a kube
step at 5h59m48s as `exit_status -1` with an empty log, whatever
`timeout_in_minutes` says, so a launcher permitted to wait past that never gets
to report why. Queueing longer than the budget belongs in Buildkite instead: a
step held by a `concurrency_group` is `limited`, has a null `started_at`, and
burns no clock. The launcher annotates what it is waiting for; read that before
assuming a fault.

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

## The health dashboard

`terraform output dashboard_url`, behind IAP; `dashboard_viewers` in
`prod.auto.tfvars` says who gets in. It is the page Buildkite cannot give the
kube fleet, where every step is on the one `kube` queue until the launcher picks
a Kueue queue for it, and where a step shows as running from the moment its
agent pod starts.

**Overview** (`/`), from the same snapshot as Live: a diagram of the
path a step takes - Buildkite, the manager's agent pods, Kueue and MultiKueue,
then one box per worker with its node pools - with how many steps are at each
stage now. Dots move along a path while steps are past it. Each box's mark is
the worst of the Health checks that belong to it, and clicking it opens that
component's details below the diagram. The glossary and what each data
source reads, and what to check when one fails, live here. Only what the
dashboard reads live is drawn, so it cannot go stale the way a diagram in a
doc does.

**Live** (`/live`), refreshed every minute:

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
  pending. Only the head of a BestEffortFIFO queue carries a reason; the rest
  are counted as queued behind it.
- **Cluster problems** - events from every cluster's `buildkite` namespace
  that are the fleet's fault (failed creates, failed scale-ups, mount
  failures, evictions, OOM kills), repeats grouped. Routine warnings - Kueue's
  backlog, scheduling while a pool scales, agent teardown, test failures - are
  collapsed beneath.

**Trends** (`/trends`; `/history` still works), for the last 6 hours, 24 hours, 7 days or 30 days (`/api/history` also takes `?start=&end=` in epoch seconds, up to 90 days):
chips admitted against chips busy (TensorCore duty) per cohort, outcomes per
queue and per pipeline from `kube_workload_timing` split into test and
infrastructure failures, wait, startup and run percentiles, and per-queue usage
and backlog. **Node autoscaling** compares topologies: nodes created, node
lifetime, peak nodes, chip-hours on nodes and the share workloads held, and
admitted → running, which includes the wait for a scale-up. Each chart point is the step's average, taken over the fleet-wide
value at each minute - collapsed to the newest Kueue controller pod and summed
over nodes first, so a controller roll or a node that came and went does not
inflate it. Node history comes from GKE's per-node metrics, which name a node
and nothing else; the GKE API's list of node pools ties each name to its pool
through the pool's instance group, which outlives the nodes it creates.

Trends' chip utilization cards also stack each topology's admitted chips under
the cohort's quota, so chips moving between shapes show as bands trading height.

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
Trends' step (`fleet.fetch_waits`), since an hour that queued in its first half
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
line under them sums up what they leave. Each queue card on Live and Trends links to its queue's jobs.

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

## When a step is stuck

Work down from admission. Everything here is on the manager unless it says
otherwise.

```bash
# Is it admitted, and if not, why?
kubectl get workloads -n buildkite
kubectl describe workload -n buildkite <name>

# Is there quota for the shape it asked for? Check borrowing too: a shape with
# nominalQuota 0 is admitted only out of its cohort's idle chips.
kubectl get clusterqueue

# Admitted but nothing running: it is on the worker that owns that shape -
# tpu-ci-us-east5 for ct6e, tpu-ci-us-central1 for tpu7x.
gcloud container clusters get-credentials <cluster> \
  --region <region> --project cloud-ullm-inference-ci-cd
kubectl get pods -n buildkite
```

Admission only means the chips are reserved. The pod still has to be scheduled,
the node possibly created from zero, and the image pulled — tens of minutes on a
cold pool. The launcher reports where it is in that gap; a step sitting quietly
at "waiting for a node" is usually the autoscaler, not a fault.

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
