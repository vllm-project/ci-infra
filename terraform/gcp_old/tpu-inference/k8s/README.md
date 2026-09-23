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
TTL, and the retry rules that let a pod survive its node being repaired. The
memory request comes from the shape's profile, since it is a fraction of the
host the pod landed on.

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

All three counts are in **nodes**, not chips — the generator multiplies
`nominal_nodes` by the machine type's chips per VM to get the ClusterQueue's
`nominalQuota`. Summed across a cluster, `nominal_nodes` should come to the chips
the reservation actually has free; `max_nodes` deliberately oversubscribes so a
shape can borrow, and `min_nodes` is the only one that really partitions the
reservation, since those chips stay with one shape once booted.

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

**Two of the three v7x shapes have no quota of their own.** All eight chips are
the nominal quota of `tpu7x-standard-4t-2x2x1`; `tpu7x-standard-1t-1x1x1` and
`tpu7x-standard-4t-2x2x2` have zero and run entirely on what that queue is not
using. Eight chips will not divide three ways and still leave each shape a whole
slice, so this is deliberate — but it means a single-chip step can wait behind a
four-chip one indefinitely, and `reclaimWithinCohort: Never` will not preempt to
free it. If a shape is starving, the lever is the split in `prod.auto.tfvars`,
not the node pools.

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
