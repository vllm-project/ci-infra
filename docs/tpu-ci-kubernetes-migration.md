# TPU CI on Kubernetes: bare-metal agents → GKE + Kueue

Moving `tpu-inference` CI off long-lived Buildkite agent VMs and onto GKE node pools scheduled by
Kueue, in `cloud-ullm-inference-ci-cd`, under the `vllm` Buildkite org.

**Audience:** engineers working on `tpu-inference`, and whoever is on the hook for CI capacity.
Sections 1–3 are the shape of the thing; sections 4–9 are the work.

**Last updated:** 2026-09-10. **Status legend:** ✅ done · 🔄 in progress · ⬜ not started

Predecessor: [`buildkite-vllm-org-migration.md`](buildkite-vllm-org-migration.md) — the
`tpu-commons` → `vllm` org move, cut over 2026-09-04. This document assumes that is done.

---

## 1. Summary

The proof of concept works. Five consecutive full v6e suites ran green on GKE (kube-dev builds
255–260, 16/16 steps, 169.4 min against 176.5 min on bare metal), and a four-host JobSet slice ran
vLLM at TP=16 (build 272). What it does not have is a path to production: it was built against a
different project, a different zone, a different reservation and a different Buildkite org from the
ones we now run on, in two PRs totalling ~5,800 lines that nobody can reasonably review.

The plan is therefore **not** "merge the PoC". It is:

1. **Tear the PoC resources down** (§4) — they are all in one Terraform state and none of them are
   in the right place. Keep #438 / #467 open and untouched as the reference.
2. **Re-land the same design as eight scoped PRs** (§5), each of which applies on its own and leaves
   a working world, so "reproducible from an empty project" is a property we test rather than claim.
3. **Fix IAM while we are in there** (§6). The TPU and CPU agent VMs currently run as the project's
   default compute service account, which holds `tpu.admin`, `storage.admin` and project-wide
   `secretmanager.secretAccessor`. Any CI job on any agent can delete the fleet or read every secret.
4. **Run the kube lane as a non-blocking nightly on v6e** (§7), funded entirely by the 26 spare v6e
   chips, so its worst case is "the dry run is red", never "CI is starved".
5. **Move `queue: cpu` into the cluster** (§8) with zero pipeline edits, by running a second
   agent-stack-k8s controller on the same queue name and draining the VMs underneath it.
   `cpu_64_core` stays on dedicated VMs — it builds Docker images.
6. **Convert v7x shape by shape** (§9), starting with the **P/D disaggregation benchmark** — already
   a GKE workload, already holding 8 chips of the v7x reservation in two hand-made pools, and not a
   merge gate. Converting it costs the fleet nothing and puts those 8 chips under Kueue. Everything
   after it is funded by the fact that a Kueue pool at `min_nodes = 0` returns its chips between
   jobs where a VM never does.

**The headline for planning purposes:** there is no cutover window. Every step in this plan is
reversible by one Terraform variable, and at no point does the blocking CI suite depend on
Kubernetes. That is the deliberate trade against the previous migration, which bought speed with a
four-hour outage. This one buys safety with a longer calendar.

---

## 2. Where we are today

### 2.1 Capacity ledger

Verified against the live reservations and agent census on 2026-09-08.

**v6e — `us-east5-a`, reservation `cloudtpu-20260828173000-731402396`, 128 chips**

| Consumer | Shape | Agents | Chips |
|---|---|---:|---:|
| `tpu_v6e_queue` | `v6e-1` | 30 | 30 |
| `tpu_v6e_8_queue` | `v6e-8` | 9 | 72 |
| **Free** | | | **26** |

**v7x — `us-central1-c`, reservation `cloudtpu-20251114223000-2002888989`, 128 chips**

| Consumer | Shape | Agents | Chips |
|---|---|---:|---:|
| `tpu_v7x_2_queue` | `tpu7x-2` (1×1×1) | 16 | 16 |
| `tpu_v7x_8_queue` | `tpu7x-8` (2×2×1) | 18 | 72 |
| `tpu_v7x_16_queue` | `tpu7x-16` | 2 | 16 |
| `tpu_v7x_32_queue` | `tpu7x-32`, **not in Terraform** | 1 | 16 |
| `tpu-v7x-cluster-c` | GKE, 2 × `tpu7x-standard-4t`, the disagg benchmark | — | 8 |
| **Free** | | | **0** |

Two things to note. `tpu7x-N` counts TensorCores, not chips — `tpu7x-8` is `2x2x1`, four chips, one
host — which is why the fleet looks like 240 chips if you read the names as v6e names. And the
ledger closes at exactly 128 only once you include `tpu-v7x-cluster-c`, the GKE cluster the P/D
disaggregation benchmark runs on: created 2026-04-08, two fixed single-node `tpu7x-standard-4t`
pools with `SPECIFIC_RESERVATION` affinity on the CI reservation, no autoscaling, and no Terraform
anywhere in this repo. Those 8 chips are the v7x pilot budget — see §9.2.

**CPU**

| Queue | Machines | Where | Used by |
|---|---|---|---|
| `cpu` | 8 × `e2-standard-2` | `us-central1-b` | 441 `queue: cpu` references in `tpu-inference/.buildkite` |
| `cpu_64_core` | 4 + 4 × `n2d-standard-64` | `us-central1-b`, `us-central1-f` | Docker image builds |

Eight `e2-standard-2` against 441 references is the sharpest capacity mismatch in the fleet, and it
is the one queue where the kube answer is unambiguously better: those steps are `bash` and `yq`, not
TPU work.

### 2.2 What the PoC actually built

One Terraform root — `terraform/gcp_old/tpu-inference/k8s`, state at
`gs://cloud-ullm-inference-ci-cd-tf-state/buildkite-tpu-ci/foundation/default.tfstate`, serial 188,
37 resource blocks. Full inventory in [Appendix A](#appendix-a--poc-resource-inventory).

The design is sound and this plan keeps all of it:

- One **manager** cluster running a single agent-stack-k8s controller on a shape-agnostic queue,
  Kueue, and JobSet. One controller, so two clusters cannot race for the same Buildkite job.
- One **worker** cluster per region, joined by MultiKueue over Connect Gateway, with TPU node pools
  that autoscale from a reservation.
- A **launcher** in a CPU-only pod that submits the real workload, so the Buildkite agent lives
  *outside* the Kueue workload: a preempted run pauses and resumes rather than failing the build,
  and an admission wait holds a CPU pod rather than a TPU.
- The **profile registry** is generated from the same tfvars that build the node pools, so placement
  cannot drift from the queues; the **workload manifest** lives in the repo under test, so the shape
  of a job is a PR rather than an infrastructure change.

### 2.3 Why it cannot be adopted in place

| PoC | Production |
|---|---|
| Worker in `cloud-tpu-inference-test` / `southamerica-west1-a` | `cloud-ullm-inference-ci-cd` / `us-east5-a` |
| 18 chips borrowed from a shared 256-chip team reservation | 26 chips of our own reservation |
| Buildkite `tpu-commons` org, CI-DEV cluster, `kube-dev` pipeline | `vllm` org, TPU cluster `0219f117` |
| Analytics token read from `cloud-tpu-inference-test` | `vllm_buildkite_analytics_token` in the CI project |

Every one of those is a tfvars change, but together they mean the running resources are worth
nothing and the state file is worth less than the confidence of rebuilding from scratch. Rebuilding
is also the only honest test of requirement 3: *the infra changes are reproducible.*

---

## 3. The approach

Three rules, and everything below follows from them.

**Kubernetes is never on the critical path until it has been boring for a month.** The kube lane
runs as a soft-failing nightly on chips that the blocking suite does not own. If GKE has a bad day,
the blocking suite does not notice.

**Every step is one variable in each direction.** Converting *k* agents of a shape means
`instance_count -= k` in the VM root and `nominal_nodes += k` in the kube tfvars. Reverting is the
same two numbers swapped. There is no state to migrate, no data to move, and no window.

**Chips are conserved at peak and won back at idle.** A bare-metal agent holds its chips 24/7. A
Kueue pool with `min_nodes = 0` holds them only while a job runs. So a chip-neutral conversion is
actually chip-positive on average, and the slack it creates funds the next batch. This is what makes
a gradual v7x migration possible without extra hardware — see §9.

---

## 4. Workstream 1 — turn down the PoC ✅ (2026-09-08)

All 37 resources are in one state, so this is one `terraform destroy` plus three manual items.

```
git worktree add /tmp/poc-teardown pr467          # the code that built it
cd /tmp/poc-teardown/terraform/gcp_old/tpu-inference/k8s
terraform init
terraform plan -destroy -out=teardown.plan        # expect 37 destroys, 0 changes
terraform apply teardown.plan
```

**Before running it,** confirm the plan destroys nothing outside the inventory in Appendix A. Two
resources reach into `cloud-tpu-inference-test` and one grants on a secret there
(`tpu_commons_buildkite_analytics_token`); those are PoC grants and should go, but they are worth
reading in the plan rather than trusting.

Then, by hand:

1. Delete the state object and prefix once the destroy is clean:
   `gs://cloud-ullm-inference-ci-cd-tf-state/buildkite-tpu-ci/`. Keep the bucket — §5 reuses it.
2. Delete the orphan service account `tpu-ci-external-secrets@cloud-ullm-inference-ci-cd` — it
   exists, holds no bindings, and is not in the state.
3. Delete `terraform/gcp_old/tpu-inference/cloud-ullm-inference-ci-cd/errored.tfstate` from the
   working tree — 706 KB of plaintext secrets, untracked, stale. (Step 43 of the previous doc,
   still outstanding.)

Buildkite-side PoC leftovers — the CI-DEV cluster `7d36687d` and the `kube-dev` pipeline — live in
`tpu-commons` and are covered by that org's teardown, not this one.

**What this returns:** 18 chips to the shared `southamerica-west1-a` reservation, three
`e2-standard-4` manager nodes, two Cloud NAT gateways, two GKE control planes, and two buckets.

**PRs #438 and #467 stay open and untouched.** They are the reference the split PRs are cut from,
and the only place the validation evidence is written down.

**What actually happened.** The plan came out at 38 destroys, matching Appendix A, and 34 applied
cleanly — including everything in `cloud-ullm-inference-ci-cd`. Four failed and were finished by
hand:

- `google_gke_hub_membership.worker` and the worker cluster behind it. Terraform destroyed
  `roles/gkehub.serviceAgent` on the worker project first, after which the hub service account could
  no longer reach the cluster it was trying to unregister. The missing `depends_on` is written up
  above §6.1 and is a required fix in PR 2.
- Both cache buckets: `force_destroy = false` against 36 GB of compilation cache and 356 GB of
  models. Deliberate on the PoC's part and correct for a bucket holding real data, but it means a
  teardown is never one command. Whatever PR 4 creates should keep `force_destroy = false` and the
  runbook should say to empty the buckets first.
- `kubernetes_namespace_v1.buildkite_manager` hung terminating for five minutes and was dropped from
  state; the cluster it lived in was deleted immediately afterwards, which removed it.

---

## 5. Workstream 2 — eight reviewable PRs ⬜

Cut from #467 (which contains #438). A git-spice stack; infra-y changes go down, launcher-specific
at the tip. Each PR is applied to the live project before the next is opened, and each has an
acceptance check that does not depend on anything above it.

| # | Scope | ~Lines | Applies to | Acceptance check |
|---|---|---:|---|---|
| 1 | ✅ **Manager cluster** ([#521](https://github.com/vllm-project/ci-infra/pull/521), merged `fb5c2f7`, applied 2026-09-09). Autopilot; `backend.tf`, `versions.tf`, `providers.tf`, `variables.tf`, `locals.tf`, `manager.tf`, manager node SA + roles from `iam.tf`, router/NAT, tfvars manager block | 273 | new state prefix `terraform/cloud-ullm-inference-ci-cd-k8s-state` | **passed** — RUNNING on 1.35.7-gke.1150000, fleet READY, no drift, nodes on `tpu-ci-mgr-node@` not `*-compute@` |
| 2 | **Worker cluster + TPU node pools.** Worker half of `clusters.tf` incl. multi-host slice validation, Fleet registration, worker IAM, per-repository AR reader for both node SAs (replacing the manager's project-wide grant from PR 1), tfvars `worker_clusters` for `us-east5-a` | 447 | same state | ✅ **PR #522, applied 2026-09-09.** Scaled `v6e-1-1x1` to 1: node `Ready` in **119 s** with `google.com/tpu: 1` and `SPECIFIC_RESERVATION` affinity; reservation in-use 102 → 103 → 102. Plan clean after. |
| 3 | ✅ **Kueue + JobSet + MultiKueue** ([#526](https://github.com/vllm-project/ci-infra/pull/526), merged `995dfb3`, applied 2026-09-09). No Terraform provider and no Helm: `scripts/deploy_manifests.py` fetches the pinned upstream `manifests.yaml` for both projects and server-side-applies `kueue/generated/` over it. Both controller configs, the auth-plugin overlay, queue/flavor/multikueue templates, `generate_manifests.py`, and the one Terraform resource this needs — `gatewayEditor` for the manager's Kueue KSA, which had to stay **project-scoped**: Connect Gateway checks `gkehub.gateway.get` against its own `projects/<project>/gkeMemberships/<id>` resource, not the fleet membership, so a membership-scoped grant is silently ineffective and surfaces only as `ClientConnectionFailed: ... unknown`. | 1,283 | same state | **passed** — a hand-written Job posted to the `ct6e-standard-1t-1x1` LocalQueue in the `buildkite` namespace on the manager ran on a worker TPU node. One queue per shape, all of a generation's queues in one cohort over one bare ResourceFlavor: `nominalQuota` is chips a shape can always have, so the shapes cannot starve each other, and the cohort lends out whatever is idle. `reclaimWithinCohort` stays `Never` — a lender waits for the borrower's jobs to finish rather than evicting them, because eviction returns the accounting instantly but not the hardware, and chips freed across the wrong node shape have to scale down before the right one can boot. Only the flavor is per generation: quota and borrowing are counted per flavor, and a flavor per shape would strand idle capacity for real. That Job carries the `gke-tpu-accelerator`/`gke-tpu-topology`/`gke-accelerator-count` nodeSelectors and the `google.com/tpu` toleration itself — the flavor supplies none of them (D7), and GKE Warden rejects a TPU pod missing the first two. `gke-accelerator-count` is what distinguishes one `ct6e-standard-8t` from two `ct6e-standard-4t` at the same `2x4` topology; see [#525](https://github.com/vllm-project/ci-infra/pull/525). The profile registry that fills them in automatically does not arrive until PR 7. |
| 4 | ✅ **Caches and workload identity** ([#529](https://github.com/vllm-project/ci-infra/pull/529), merged `f819f4f`, applied 2026-09-09). `cache.tf`, `cache_volumes.yaml.tpl`, `workload_sa.yaml.tpl`, bucket IAM, the gcsfuse CSI addon on workers, `namespace` into the tfvars, generated `workload/` per worker | 663 | same state | **passed** — a pod running as `tpu-workload` wrote and read back through both mounts, and the two writes landed in *different* buckets. Two buckets rather than two prefixes of one: the driver keys a volume by `volumeHandle`, which is the bucket name, so two PersistentVolumes over one bucket are a single mount and `/cache/jax` lists the model cache. Bucket names are **derived**, `<prefix>-<purpose>-<sha256(project/region)[:8]>` — a bucket name is globally unique across GCP, so the project has to be in it, and there is then nothing left to declare. `locals.tf` and `generate_manifests.py` derive it separately (one creates the bucket, the other writes the `volumeHandle`) and two derivations can drift, so `deploy_manifests.py` refuses to deploy until every bucket a volume names exists — left to Kubernetes it never surfaces, since a volume naming a bucket nobody created applies cleanly and fails later at mount time. |
| 5 | ✅ **Secrets** ([#530](https://github.com/vllm-project/ci-infra/pull/530), merged `61896bb`, applied 2026-09-10). GKE `SecretSync`, not External Secrets — so no ESO Helm release and no `tpu-ci-external-secrets@`. `secret_sync.yaml.tpl` (sync KSA + `SecretProviderClass` + `SecretSync`) into a new generated `workload/` on the manager, and one `google_secret_manager_secret_iam_member` on the agent-token secret. **Manager only** — and not because a worker does no Buildkite work. A TPU pod uploads its own artifacts, writes meta-data and reports to Test Engine, because it is the only thing that can see its own output files. It does so with `BUILDKITE_AGENT_ACCESS_TOKEN`, the per-job credential the agent is handed back after registering, which the launcher forwards as a plain env var and which dies with the job. This secret is the *org registration* token, and the only things that register are the agent-stack-k8s controller and the agent pods it creates, both on the manager | 110 | same state | **passed** — an `Opaque` Secret `buildkite-agent-token` in `buildkite` on the manager, key `BUILDKITE_AGENT_TOKEN`, owned by the SecretSync, conditions `CreateSucceeded` / `UpdateNoValueChangeSucceeded`. The worker has no `secretsync` CRD at all — `secret_sync_config` is off there — so manager-only is enforced by cluster config and not only by what the generator renders. |
| 6 | ✅ **Buildkite controller** ([#531](https://github.com/vllm-project/ci-infra/pull/531), merged `bd0e421`, applied 2026-09-10). agent-stack-k8s 0.49.0 on the manager, on queue `kube`, in the `buildkite` namespace PR 3 already creates — the controller, its launcher pods and the LocalQueues those pods submit against all have to share one namespace, because a LocalQueue is namespaced. **No `buildkite.tf` and no Helm release**: PR 3 dropped the Terraform Helm and Kubernetes providers because they want a reachable API server at plan time, so `helm` is used as a renderer only — `helm pull` then `helm template`, piped through the same server-side apply as the Kueue and JobSet releases. The values are generated and committed; the rendered output is not, because it would depend on the renderer's helm version and break the drift check for someone who changed nothing. Three lines of values: the chart pastes them under `agent-token-secret`, `namespace` and `id` of its own and the controller's decoder rejects a duplicated key, and the org is not named at all — since 0.28.0 the controller takes the org and the cluster from the agent token | 120 | same state | **passed** — the controller registers as `cluster-name=TPU organization-slug=vllm queue=kube`, and a two-build smoke pipeline on that queue passed end to end. Autopilot's admission mutation is not in what we send, so the first apply bumps the Deployment's generation without rolling it; it settles on the second |
| 7 | ✅ **The launcher.** Applied 2026-09-10. `kueue/launcher/launch.py`, `launcher.yaml.tpl`, `launcher_rbac_worker.yaml.tpl`, the profile registry in the generator, launcher IAM, `allowed_image_repos`, the analytics-token grant, and generated `manager/workload/10-launcher.yaml` + `workers/*/workload/20-launcher-rbac.yaml`. Profiles are named `<machine_type>-<topology>`, so `ct6e-standard-8t-2x4` rather than `v6e-8-2x4`. The launcher runs its own image, `kueue/launcher/Dockerfile` — the Cloud CLI image plus PyYAML, which that image ships only as a Python 2 copy whose imports fail; built by hand into the `tpu-ci` Artifact Registry repo and tagged `<cli-version>-<dockerfile-revision>`. Two things the manager being **Autopilot** forces: it injects a `cloud.google.com/extended-duration-pods` nodeAffinity into any podspec that has none, and MultiKueue copies it to the Standard worker where no node carries that label, so the pod is unschedulable with nothing reporting an error — the launcher states an affinity of its own to suppress it. And the launcher pod itself needs pod-level `fsGroup: 1000`, because agent-stack-k8s forces `runAsUser: 0` on its own `copy-agent` and `checkout` containers and the UID-1000 command container has to write the workspace they created | 1,600 | same state | **passed** — `launch --profile ct6e-standard-1t-1x1 -- python -c 'import jax; print(jax.devices())'` from a Buildkite step on queue `kube` printed `[TpuDevice(id=0, process_index=0, coords=(0,0,0), core_on_chip=0)]`: submitted on the manager, admitted by Kueue, dispatched by MultiKueue to `tpu-ci-us-east5`, node scaled up from zero, logs streamed back over Connect Gateway and uploaded as a per-Job artifact. The command is joined with `shlex.join`, since the step's shell has already split it and the manifest runs the result through a shell again |
| 8 | **README and runbook.** | 200 | — | — |

Four notes on the split.

**Worker clusters are regional, identified by project and region, with nodes pinned by
`node_locations`.** The tfvars lists them rather than keying them, because that pair is the whole
identity and every name is derived from it: project-scoped names (cluster, node SA) take the region
alone, fleet-wide ones (Terraform address, generated directory, `MultiKueueCluster`) take both.
Networking is the exception and is not derived, because it is not this fleet's: a NAT gateway covers
every subnet range in its region and network, so a second one over ranges another already claims is
refused at apply, and a region can therefore hold exactly one. It is created by hand and named for
what it serves rather than for us — `default-us-central1-router`/`-nat`, `default-us-east5-router`/`-nat`,
both renamed out of the `tpu-ci-*` names on 2026-09-10 — and the runbook says to check for an
existing one before creating. One limit to know before it bites — a GKE cluster name is also its Fleet membership ID
and every worker joins the *manager's* fleet whatever project it runs in, so two clusters in one
region in different projects would collide; the fix is an optional short name on one of them, which
renames nothing existing when it is added. A
cluster's `location` fixes the control plane; `node_locations` fixes where nodes go. They are
independent, so a zonal TPU reservation constrains only the second. Google's TPU-on-GKE guide says
to "use regional clusters, which provide high availability of the Kubernetes control plane" and
name the reservation's zone in `--node-locations`. Set `node_locations` at cluster level as well as
per pool: only the cluster-level default reaches a pool GKE creates on its own. In a regional
cluster the per-zone `min_node_count`/`max_node_count` are multiplied by the number of zones, so
use `total_min_node_count`/`total_max_node_count`. Cluster location cannot be changed in place.

**Reference architecture: `bodaborg-tpu7x-nap`** in `cloud-tpu-shared-capacity`, a possible merge
target (§9). Inspected 2026-09-09, and it is the pattern this plan is converging on: regional
`us-central1` control plane, cluster-level `locations: [us-central1-c]`, Standard, RAPID,
1.36.0-gke.3712000. Despite the name, `clusterAutoscaling` is **empty** — classic NAP is not
enabled. Five pools are `autoprovisioned: true` from ComputeClass `nodePoolAutoCreation`, and they
carry everything classic NAP cannot express: `SPECIFIC_RESERVATION` affinity on a named
reservation, the `google.com/tpu` taint, and a multi-host placement policy
(`tpu7x-512-4x8x8-placement-policy`, topology `4x8x8`). Hand-declared pools sit alongside them:
`default-pool` (e2-standard-16) and `cpu-np` (n2d-standard-64, 30–45) — the same machine type as
our `cpu_64` queue, which matters for §8. The ComputeClass manifests themselves could not be read;
the control plane is behind authorized networks. Get them before acting on §9.

**Two things that cost an apply in PR 2; do not repeat them in 3–8.** First, do not declare a
`google_gke_hub_membership` for a cluster that already has a `fleet {}` block. GKE registers the
cluster itself and creates the membership *in the cluster's region*, so an explicit resource asks
for `locations/global` and fails with "already registered with resource: .../locations/us-east5/…"
rather than adopting it. PR 1's manager had this right; the worker copied a pattern from the PoC.
Second, on every TPU node pool, ignore `node_config[0].advanced_machine_features`. GKE turns SMT
off on a TPU host and reports `threads_per_core = 1` back; untracked, Terraform reads that as
"remove the block", which is ForceNew — a routine plan that silently rebuilds a pool and drops its
chips.

**The manager is Autopilot; the workers are Standard.** Decided 2026-09-08, in PR 1, because
`enable_autopilot` is ForceNew and cannot be revisited after the first apply. The manager runs ~2
vCPU of controllers plus one launcher pod per in-flight step — a bursty shape that a regional
Standard pool has to floor at one node per zone and pay for at idle. Autopilot's refusal of
privileged containers, hostPath and host networking also makes "no real workloads on the manager" a
platform property rather than a convention. Workers cannot follow: TPU pools need reservation
affinity on a named reservation, the `google.com/tpu` taint, COMPACT placement with an explicit
topology and whole-slice atomic scaling, which is precisely what Autopilot and node
auto-provisioning take away. One consequence to carry forward: Autopilot still runs its nodes as a
service account and defaults to the project's over-privileged compute SA unless
`cluster_autoscaling.auto_provisioning_defaults.service_account` says otherwise.

This does **not** constrain §8. Autopilot rejects privileged containers, so `docker:dind` cannot run
on the manager — but nothing except controllers and launcher pods was ever going to, and §8.1
settles image builds onto Cloud Build, which needs no privilege anywhere. (Mounting the host Docker
socket is unavailable on Standard too: GKE nodes have run containerd since 1.24.)

**PR 5 should try GKE SecretSync before reaching for External Secrets Operator.** PR 1 enabled
`secret_manager_config` and `secret_sync_config` on the manager, and both are confirmed live: the
`csi-secrets-store-gke` and `csi-secrets-store-provider-gke` daemonsets are Running, and the CRDs
`secretproviderclasses.secrets-store.csi.x-k8s.io` and `secretsyncs.secret-sync.gke.io` are
installed. `SecretSync` materialises a Secret Manager secret as a native Kubernetes `Secret`, which
is exactly the job the PoC gave ESO — agent-stack-k8s wants the Buildkite agent token as a `Secret`,
not as a mounted file.

If it works, PR 5 drops a third-party controller *and* the `tpu-ci-external-secrets@` service
account, which is already on the §4 teardown list as an orphan.

✅ **Tested 2026-09-09 on the manager, and it works — use `SecretSync`, not ESO.** A
`SecretProviderClass` plus a `SecretSync` in a scratch namespace, with `secretmanager.secretAccessor`
granted on that one secret to `…svc.id.goog[<ns>/<ksa>]`, produced an `Opaque` Secret within 20s of
apply. Both open questions came back fine:

- **Rotation updates the Secret in place.** Adding a new Secret Manager version changed the value of
  the same Secret object — no delete and recreate, no new name — within 270s of the write. There is
  no watch: the controller polls, and reports `UpdateNoValueChangeSucceeded` on the quiet passes, so
  treat five minutes as the propagation bound. That is fine for the launcher pods, which read the
  token at pod start, but the agent-stack-k8s *controller* holds its copy for the life of its
  process, so rotating the token still means restarting that Deployment. Worth a line in the PR 8
  runbook.
- **`SecretSync` writes only into its own namespace** — `spec` has no target-namespace field at all.
  Not a constraint in practice: the Secret is wanted in `buildkite`, which is where the SecretSync
  will live.

Two things the test turned up that the PR has to account for. The controller is **not a workload on
the cluster** — no Deployment, no DaemonSet, nothing in `kubectl get pods -A` beyond the two
`csi-secrets-store` DaemonSets — so it is GKE-managed, its poll interval is not ours to tune, and
there is nothing for `deploy_manifests.py` to install or wait on. And the org policy
`constraints/gcp.resourceLocations` rejects `automatic` replication in this project: any secret
Terraform creates needs an explicit `user_managed` replica, which is what the existing
`vllm_buildkite_agent_token` already uses (`us-central1`).

**PR 7 is big and should stay big.** 990 of its lines are one Python program; splitting a program in
half across two PRs makes both unreviewable. But another ~990 lines are `generated/06-launcher.yaml`
carrying a second verbatim copy of that program as a ConfigMap. **Worth changing while we are
re-landing it:** have `deploy_manifests.py` build that ConfigMap with
`kubectl create configmap --from-file=launch.py --dry-run=client -o yaml | kubectl apply -f -`
instead of committing the duplicate. That halves the diff, and removes a class of drift where the
template and the script disagree.

**The `generated/` directory is a drift detector, so enforce it.** Add a ci-infra check that runs
`generate_manifests.py` and fails on a non-empty `git diff`. Today the invariant is written in the
README and nothing enforces it.

**Settled in PR 6: the registration token does not reach the workload pod.** `run.sh` in
[#3377](https://github.com/vllm-project/tpu-inference/pull/3377) forwards *every* `BUILDKITE_*`
variable it can see into the workload pod, minus a four-name deny list, and that sweep is
deliberate — a hand-maintained list of a hundred names kept losing entries, and
`BUILDKITE_ANALYTICS_TOKEN` went missing for months. The sweep is safe only if
`BUILDKITE_AGENT_TOKEN` is *not* in the command container's environment. Checked against a real
agent pod: it is not. The command container's environment holds `BUILDKITE_AGENT_ACCESS_TOKEN` and
`BUILDKITE_AGENT_JOB_API_TOKEN`, both scoped to the one job, and `BUILDKITE_AGENT_TOKEN` appears
only on the `agent` container — where it is a per-job acquisition token the controller mints into
its own `buildkite-job-token-<uuid>` Secret and deletes with the job, not the org token. Nothing to
add to `FORWARD_DENY`.

**Two things PR 6 turned up that belong in the PR 8 runbook.** Kueue's pod webhook has
`failurePolicy: Fail` and does not exclude the `buildkite` namespace, so while
`kueue-controller-manager` is rolling — which `deploy_manifests.py` does on every run — pod creation
in `buildkite` fails and the ReplicaSet retries for a couple of minutes. It recovers on its own,
but a deploy is not invisible to a running fleet. And completed agent Jobs linger for
`job-ttl`, which defaults to 10m; `max-in-flight` defaults to 25, which is the ceiling on
concurrent Buildkite jobs and will matter when the `cpu` queue moves over in §8.

**Ordering is load-bearing between 3 and 4 only.** ClusterQueues naming an absent ResourceFlavor go
inactive and recover; PVCs naming an absent bucket do not.

---

## 6. Workstream 3 — IAM: codify, then narrow ⬜

Two separable problems. The second is the one that matters.

**Standing rule for everything below: grant on the resource, not on the project, whenever the
resource has an IAM policy of its own.** Least privilege is the obvious reason, but two others came
out of the teardown and are less obvious:

- **Project-level policy is not ours alone.** `cloud-tpu-inference-test` has its bindings set
  through a separate process that rewrites the project policy wholesale, so a
  `google_project_iam_member` we add there is liable to be wiped without anything in our state
  changing. A `google_secret_manager_secret_iam_member` on one secret survives, because it lives in
  that secret's own policy.
- **Project bindings create invisible destroy-ordering hazards.** The PoC teardown left the fleet
  membership undeletable: Terraform removed `roles/gkehub.serviceAgent` from the worker project
  before deleting `google_gke_hub_membership.worker`, because nothing in the config said the
  membership needed that binding. Resource-level grants tend to sit on the resource whose lifetime
  they track, so the ordering is expressed for free. Where a project binding really is required, say
  so with `depends_on` — PR 2 must add
  `depends_on = [google_project_iam_member.gkehub_service_agent]` to the membership, or the next
  teardown reproduces this exactly.

What that means for the PRs, checked against what the PoC actually declared:

| PoC binding | Rescope to | Where |
|---|---|---|
| project `secretmanager.secretAccessor` → `external-secrets` KSA (manager + worker) | `google_secret_manager_secret_iam_member` on the agent-token secret only, and to the `SecretSync` KSA on the manager rather than an ESO one — the controller is gone entirely. Verified working at this scope during the SecretSync test | PR 5 |
| project `artifactregistry.reader` → node SAs (both directions, cross-project) | `google_artifact_registry_repository_iam_member` on the five `us-central1` CI repos in `var.image_repositories`, for the manager and worker node SAs alike | done in PR 2 |
| project `gkehub.gatewayEditor` / `gatewayReader` → Kueue and launcher KSAs | **cannot be narrowed** — attempted in PR 3 and reverted. Connect Gateway evaluates `gkehub.gateway.get` against `projects/<project>/gkeMemberships/<id>`, its own resource, not the fleet membership, so a `google_gke_hub_membership_iam_member` grant looks correct in state and still gets `PERMISSION_DENIED`. Do not re-attempt when adding v7x workers | PR 3 (done), PR 7 |
| project `storage.objectUser` → workload KSA | already bucket-scoped — kept as the model, one `google_storage_bucket_iam_member` per bucket | ✅ PR 4 |
| project `secretmanager.secretAccessor` → launcher, for the analytics token | already secret-scoped — keep as the model | PR 7 |

Three have no resource-level form and stay at project scope: `logging.logWriter`,
`monitoring.metricWriter` / `monitoring.viewer` / `stackdriver.resourceMetadata.writer` (no target
resource), `gkehub.viewer` (it authorises *listing* memberships, so it cannot be scoped to one), and
`gkehub.serviceAgent` on a worker project (a service-agent grant, project-scoped by definition).

### 6.1 What is already codified

The PoC's own IAM was in Terraform — manager node SA roles, Connect Gateway workload-identity
bindings for `kueue-controller-manager` and `tpu-launcher`, external-secrets, cross-project
Artifact Registry reader, bucket `objectUser`. Those come back with PRs 1–7 and need no separate
work beyond narrowing (§6.3).

### 6.2 What is not codified

| Binding | Provenance | Action |
|---|---|---|
| `tpu-ci-external-secrets@` SA, zero bindings | PoC leftover | delete (§4) |
| `32478767326-compute@` (test-project default SA) holds `bigquery.dataEditor`, `container.developer`, `storage.admin`, `artifactregistry.reader` on the CI project | cross-project PoC grants | remove with the PoC teardown; the new worker is in-project |
| `cloud-tpu-inference-test.svc.id.goog[external-secrets/external-secrets]` → project-wide `secretmanager.secretAccessor` | PoC | remove; replace with secret-scoped bindings in PR 5 |
| `630405687483-compute@`, `735972712744-compute@` → `artifactregistry.admin` / `writer` / `storage.admin` | unknown, foreign projects | **see D2 in §11** — identify or remove |
| 9 humans with `roles/editor`, 13 with `roles/compute.admin` | shared project, predates us | out of scope for this plan, but see D3 |

### 6.3 The real problem: the agent fleet runs as the default compute SA

`modules/ci_cpu`, `ci_cpu_64_core`, `ci_v6e` and `ci_v7x` all declare
`service_account { scopes = ["cloud-platform"] }` with **no `email`**, so every agent VM runs as
`443452445451-compute@developer.gserviceaccount.com`. On this project that identity holds:

```
artifactregistry.repoAdmin   bigquery.dataEditor      bigquery.user
cloudbuild.builds.editor     compute.instanceAdmin.v1 compute.viewer
container.developer          iam.serviceAccountUser   iap.tunnelResourceAccessor
logging.logWriter            monitoring.metricWriter  secretmanager.secretAccessor
storage.admin                storage.bucketViewer     tpu.admin
```

Concretely: a test running on any CI agent can delete every TPU in the fleet (`tpu.admin`), read
every secret in the project (project-wide `secretmanager.secretAccessor` — including the Buildkite
agent token and the HF token), and rewrite any bucket (`storage.admin`). `tpu-inference` builds pull
requests from forks. This is the finding to fix first, and it is independent of everything else in
this document.

**Proposed target,** one PR per fleet kind so a mistake is one queue and not the fleet:

| Identity | Roles |
|---|---|
| `ci-agent-tpu@` (v6e, v7x) | `logging.logWriter`, `monitoring.metricWriter`, `artifactregistry.reader`, `storage.objectUser` on `ullm-ci-cache` only, `secretmanager.secretAccessor` on the three CI secrets only |
| `ci-agent-cpu@` | same, minus the cache bucket |
| `ci-agent-cpu64@` | same, plus `artifactregistry.writer` on the CI repo only (it pushes images) |

Keep `scopes = ["cloud-platform"]` — OAuth scopes are the legacy control and narrowing them breaks
`gcloud` in surprising ways; the service account roles are the real boundary.

One caveat on `ci-agent-cpu@`: the disagg benchmark runs on `queue: cpu` and calls
`gcloud container clusters get-credentials`, so it needs `container.developer` today. Do not add
that role to the new CPU identity — §9.2 removes the need for it, so sequence the disagg conversion
before the CPU SA narrowing, or grant it temporarily with a `TODO` naming §9.2.

Changing a VM's service account is `ForceNew`, so each of these rolls its fleet. Sequence them into
the existing rolling-restart procedure (`scripts/rolling_restart.py`) and respect the maintenance
window: **do not recycle `queue=cpu` agents between 00:00 and 06:30 UTC**, when the disagg benchmark
strands v7x TPUs if its agent dies. (§9.2 retires that rule.)

The kube fleets get this for free: PR 1 already creates a dedicated node SA with five roles, and
workloads run under a distinct `tpu-workload` KSA that the launcher refuses to let a manifest
override.

### 6.4 Dead configuration to remove while we are here

Carried over from the previous migration's step 44, still outstanding, and each is a one-line PR:

- Unreferenced modules `{benchmark, ci_v5, ci_v6}`.
- The `cloud-tpu-inference-test-v7x` root — it declares 8 × `tpu7x-2` and 8 × `tpu7x-8` in a project
  where the census shows zero v7x. Phantom.
- Unused `us-east5-a` and `southamerica-west1-a` provider aliases in `cloud-tpu-inference-test`.
- `h100_8_queue` — zero YAML references, never ran a job.

---

## 7. Workstream 4 — the tpu-inference dry run ⬜

PR [tpu-inference#3377](https://github.com/vllm-project/tpu-inference/pull/3377) is 1,317 lines
across 10 files. It splits three ways.

### 7.1 Land now, independently of Kubernetes (~50 lines)

These are test fixes that help bare metal too and have no kube dependency. They should be one small
PR that goes in this week:

| File | Change | Why it stands alone |
|---|---|---|
| `tests/models/jax/test_gemma4.py` | load each large checkpoint once instead of round-robin | checkpoint loading in part2 falls 23.8 → 8.8 min **on bare metal** |
| `tests/e2e/benchmarking/bench_utils.sh` | stop treating a `UserWarning` quoting `OSError` as fatal | false-failure fix |
| `tests/test_envs.py` | unset `TPU_NAME`/`TPU_ACCELERATOR_TYPE` rather than assume absent | correctness; GKE injects them, bare metal does not |
| `examples/disagg/run_disagg_single_host.sh` | wait for the proxy before benchmarking | flake fix |

### 7.2 The dry-run lane (~800 lines)

| File | Keep? |
|---|---|
| `.buildkite/kubernetes/run.sh` | yes |
| `.buildkite/kubernetes/manifests/test.yaml` | yes |
| `.buildkite/kubernetes/README.md` | yes |
| `.buildkite/pipeline_kube.yaml` | yes, **v6e steps only** |
| `docker/Dockerfile` (+ `buildkite-agent` CLI) | yes — 20 lines, no behaviour change on bare metal |

Note `.buildkite/kubernetes/manifests/` already exists on `main` with `storageclass.yaml` and the
`v7x/` disagg manifests, so the directory is not new; the PR adds `test.yaml` beside them.

### 7.3 Defer

`examples/disagg/run_disagg_multi_host_pod.sh` (281 lines) — the eight-process single-host
disaggregation rewrite. It is the hardest step to port and the least valuable to port early. It
comes back when the kube lane is carrying real traffic.

### 7.4 How the dry run is wired

Add the kube group to the nightly, not to the per-push pipeline:

```yaml
x-kube-dry-run: &kube-dry-run
  if: build.env("NIGHTLY") == "1"
  soft_fail: true                 # already the idiom in pipeline_jax.yml
  agents: { queue: kube }
  plugins: [{ kubernetes: { podTemplate: tpu-launcher } }]
  timeout_in_minutes: 90
  cancel_on_build_failing: false
```

Four properties this must have, in order of importance:

1. **`soft_fail: true` on every step.** A red kube step must not turn the nightly red, or people
   will stop reading the nightly.
2. **Its own chips.** The kube profiles draw only on the 26 spare v6e chips (§9.1). It cannot
   preempt, starve, or slow the blocking suite, because it never touches the same chips.
3. **`build.env("NIGHTLY") == "1"` only.** No per-push traffic until the lane has a clean fortnight.
4. **A green/red signal somewhere people look.** `soft_fail` steps are easy to ignore. Emit a daily
   line into the existing BigQuery CI dataset (`ci_monitoring` already pulls Buildkite per build)
   and put a "kube dry run, last 14 nights" panel next to the fleet dashboard. **The exit criterion
   for §9 is a number on that panel, not a vibe.**

**Exit criterion:** 14 consecutive nights at ≥ the bare-metal pass rate for the same steps, and
per-step median execution within 10% of bare metal.

---

## 8. Workstream 5 — `queue: cpu` into the cluster ⬜

The 441 `queue: cpu` references in `tpu-inference/.buildkite` are the reason to do this carefully
and the reason not to rename anything.

**Do not rename the queue.** Run a *second* agent-stack-k8s controller on the manager cluster,
configured with `queue: cpu` — a second entry in the generator's chart list, rendered under its own
release name so its objects and its `id` do not collide with the `kube` one (two controllers
sharing an `id` can both reserve the same job). No node pool and no Kueue: these steps want no accelerator, so they
are ordinary pods, and Autopilot scales them 0 → N by itself. This is the workload Autopilot is
actually for — a few hundred short, bursty, unprivileged jobs a day. Buildkite dispatches a
job to whichever agent picks it up first; it does not care whether that agent is a VM or a pod. So
VM agents and pod agents serve the same queue side by side, and the migration is:

| Step | `cpu` VMs | kube `cpu` replicas | Risk |
|---|---:|---:|---|
| 8a | 8 | 2 | pods take a slice of traffic; if they misbehave, scale to 0 |
| 8b | 8 | 8 | queue depth halves; still fully covered by VMs |
| 8c | 4 | 12 | first real reduction, reversible in one apply |
| 8d | 0 | autoscale 4–24 | VMs gone, `ci_cpu` module deleted |

No pipeline edits at any step. Rollback at any step is `instance_count`.

One dependency: **do §9.2 first.** The disagg benchmark holds a `queue: cpu` slot for up to 630
minutes and leaks TPU pods if that agent dies, which is why CPU capacity cannot be disrupted between
00:00 and 06:30 UTC. Draining CPU VMs under that constraint is needlessly delicate; converting the
benchmark first removes both the long-held slot and the constraint.

Two things to get right before 8a:

- **The pod needs what the VM had.** The `ci_cpu` startup script installs `yq`, `minijinja-cli`,
  `bk`, Docker, the GitHub App credential helper, and `HF_TOKEN`. Build a `ci-cpu-agent` image with
  everything but Docker.

  Docker turns out to be needed by exactly **one** step on this queue: `nightly_releases.yml`, which
  runs `docker system prune -a`, `docker build` and `docker push` for `vllm/vllm-tpu:nightly`.
  Everything else on `queue: cpu` is coordination — coverage combine and report and
  `check_results.sh` in `pipeline_jax.yml`, `nightly_verify.yml`, `integration_promote.yml`,
  `validate_benchmark_case_name.yml`, the parallelism and features report steps, and
  `pipeline_disagg_gke.yml`, which needs only `gcloud` and `kubectl`. Send `nightly_releases` to
  Cloud Build per §8.1; it does not gate anything else here, so it can also just stay on the last VM
  until 8d if §8.1 has not landed.
- **The GitHub App credential helper** currently reads a PEM from `buildkite-agent secret get`. In a
  pod that becomes an External Secret plus the same helper script; the mechanism is unchanged.

**`cpu_64_core` stays on dedicated VMs — for now, and for weaker reasons than expected.** It runs
`setup_docker_env.sh` and `publish_nightly_images.sh`: Docker builds pushing to Artifact Registry.

The obvious objection to moving them is that a pod would lose the local layer cache. It would not,
because there is no local layer cache to lose. `setup_docker_env.sh:76` runs `docker builder prune
-f` before every build, and the build itself is

```
docker build --build-arg VLLM_COMMIT_HASH=... --no-cache -f docker/${DOCKERFILE_NAME} ...
```

`--no-cache`, deliberately, on every path. What passes for caching here is at whole-image
granularity in the registry: the build tags `${CI_IMAGE_REPO}:${CACHE_TAG}` and pushes it, and a
later step with `USE_PREBUILT_IMAGE=1` pulls that tag instead of building at all. That mechanism is
registry-side and survives any change of builder or host. The only genuinely local state a build
depends on is the `python:3.12-slim-bookworm` base layer and a 118 MB context — a cold pod pays
seconds for both, against a 6–10 minute build.

So these builds are portable. What they are not, yet, is *scheduled* — and that is the actual
finding. Over the last 30 days (`ci_efficiency_metrics.step_execution_logs`):

| step | runs | median | total |
|---|---:|---:|---:|
| tpu6e base image | 1010 | 9.8 min | 160.8 h |
| tpu7x base image | 1011 | 9.7 min | 158.6 h |
| vllm-torchtpu CI image | 691 | 6.9 min | 71.2 h |
| nightly publish | 60 | 18.2 min | 18.1 h |
| **total** | | | **≈391 h** |

Against 8 × `n2d-standard-64` running continuously — 5,840 VM-hours, 512 vCPU always on — that is
**≈6.7% utilization**. The queue is sized for a burst that arrives a few times an hour. This is the
strongest cost argument in the whole migration and it has nothing to do with TPUs.

Deferral therefore stands on sequencing, not difficulty: §9 needs the CPU lane stable while v7x
moves, and re-pointing the builder is a change with its own failure modes (registry auth, context
upload, ARG passthrough) that should not land in the same week. **The builder these steps move to is
Cloud Build** — see §8.1 for how that was decided and what it costs. When it lands, `cpu_64_core`
has no remaining reason to exist and the VMs go away with it, which is the largest single line item
in this whole migration.

### 8.1 Building images without a Docker daemon

Docker-in-Docker needs a privileged container, which Autopilot forbids. That is a real constraint,
but it is not the constraint it looks like: GKE nodes have run containerd since 1.24, so mounting a
host Docker socket is unavailable on *Standard* too. Every option below is a way to build an OCI
image from a pod without a Docker daemon on the host.

What Autopilot actually enforces, from its security-capabilities reference, since the rest of this
section turns on the detail:

- `privileged: true` is blocked outright (verified Google Cloud partners excepted).
- Capabilities are the Baseline set — `SETPCAP, MKNOD, AUDIT_WRITE, CHOWN, DAC_OVERRIDE, FOWNER,
  FSETID, KILL, SETGID, SETUID, NET_BIND_SERVICE, SYS_CHROOT, SETFCAP, SYS_PTRACE` — plus `NET_RAW`
  and `NET_ADMIN` as opt-ins. **`CAP_SYS_ADMIN` is not obtainable.**
- Volume types are limited to configMap, csi, downwardAPI, emptyDir, gcePersistentDisk, nfs, PVC,
  projected and secret. No device passthrough, so **no `/dev/fuse`**. `hostPath` is read-only and
  only under `/var/log`.
- `seccompProfile: Unconfined` **is** permitted as a per-workload override of the `RuntimeDefault`
  Autopilot applies. AppArmor is `docker-default` on COS and no unconfined override is documented.
- Running as root and privilege escalation *are* allowed — Autopilot meets Baseline, not Restricted.

| Approach | Privilege required | Autopilot | Verdict |
|---|---|---|---|
| `dind` sidecar | privileged | ✗ | Works unchanged on Standard. The zero-thought fallback. |
| Kaniko | none (root *inside* the container) | ✓ | **Archived.** `GoogleContainerTools/kaniko` was archived; last release v1.24.0, 2025-05-23. Do not adopt. |
| BuildKit rootful | `CAP_SYS_ADMIN` | ✗ | Not obtainable on Autopilot. Same wall as `dind`. |
| BuildKit rootless | seccomp unconfined + AppArmor unconfined; `/dev/fuse` unless the `native` snapshotter is used | ? | seccomp is available, AppArmor is the open question. **Unverified, not disproven** — one pod settles it. `native` snapshotter avoids `/dev/fuse` at the cost of copying every layer. |
| Buildah + `vfs` | none, but wants `/dev/fuse` for overlay | ~ | `vfs` avoids `/dev/fuse` by copying every layer. Correct and slow. |
| `buildx` remote driver | none **in the client pod** | client ✓ / daemon ✗ | Relocates the privilege, does not remove it: the pod holds only the client, but `buildkitd` still needs `CAP_SYS_ADMIN` and therefore a Standard node pool. |
| Cloud Build | none in the pod | ✓ | The pod runs `gcloud builds submit` and waits. |

The row to read carefully is `buildx` remote. It is easy to see "no privilege in the pod" and
conclude the daemon can live on the manager; it cannot. On Autopilot every viable option is
*delegation* — either to Google (Cloud Build) or to a Standard cluster (`buildkitd`, `dind`). That
is the honest summary, and it matches the intuition that Cloud Build "delegates to other services":
so does everything else here.

Cloud Build is delegation, as expected — the pod becomes a thin client. Its cost is not the rate
card people remember: Cloud Build now bills per vCPU-minute and GB-minute, not per machine-type
minute. us-central1, from the Billing API:

| | per vCPU-min | per GB-min |
|---|---:|---:|
| E2, default pool | $0.001600 | $0.000350 |
| N2D, private pool | $0.002017 | $0.000441 |
| C3, private pool | $0.002542 | $0.000472 |

N2D on GCE, for comparison, is $0.027502/vCPU-hr and $0.003686/GB-hr — so `n2d-standard-64` is
$2.704/hr on demand, $1.089/hr Spot. Cloud Build's private-pool N2D rate is ~2.1× the GCE rate for
identical hardware.

That premium does not decide anything, because we are paying for idle, not for rate. Applying ≈391
build-hours/month to each option:

| Option | Assumption | $/month |
|---|---|---:|
| **Today**: 8 × `n2d-standard-64`, 24/7 | 5,840 VM-h | **≈$15,800** (≈$12,600 after N2D sustained-use) |
| GKE autoscaled pool, same machine, on demand | ≈35% packing → 1,120 node-h | ≈$3,000 |
| Cloud Build private pool, 64 vCPU / 256 GB | same wall clock | ≈$5,700 |
| Cloud Build default pool, `e2-highcpu-32` | 1.3× wall clock | ≈$1,900 |
| GKE autoscaled pool, same machine, Spot | ≈35% packing → 1,120 node-h | ≈$1,200 |

Every row beats the status quo by 2–13×, so the choice is about operations, not money. Two caveats
on the numbers: they exclude boot disks, egress and the Artifact Registry storage all options share;
and the 1.3× and 35% figures are estimates, not measurements. Confirm against actual billing before
quoting any of this.

**Decision: Cloud Build, for all four build steps.**

Once "every option is delegation" is on the table, the question stops being *whether* to delegate
and becomes *what we have to operate in order to delegate*. On that axis Cloud Build wins outright:

- **No privileged surface anywhere in our clusters.** The agent pod runs `gcloud builds submit` and
  waits. The manager stays Autopilot; worker clusters keep TPU workloads as their only reason to be
  Standard. `dind` would put a privileged container next to fork-PR code, and `buildkitd` would make
  us run a long-lived privileged Deployment — both are defensible against today's VMs, but neither
  is *nothing*, and Cloud Build is nothing.
- **No stateful component.** `buildkitd`'s advantage was a shared layer cache, but §8's `--no-cache`
  finding means we are not losing a cache we have. Adopting `buildkitd` would mean taking on cache
  GC, cache disk sizing and restart-loses-cache in exchange for a benefit we would first have to go
  and create.
- **It removes the reason `cpu_64_core` exists.** These VMs are 512 always-on vCPU at 6.7%
  utilization, and Docker is the only thing keeping them. Delegating the builds deletes the node
  pool question rather than relocating it.
- **Cost is in the same band as every other option** (≈$1,900–5,700/month against ≈$12,600–15,800
  today) and it is the only one with no floor: no idle nodes, no warm pool.

What it costs us, stated honestly, since these are the things that will bite during 8.1:

- **Log streaming and cancellation are not free.** `gcloud builds submit` streams logs, but a
  Buildkite cancel kills the client, not the build. Needs an explicit `gcloud builds cancel` in a
  trap — the same shape of bug as the disagg benchmark's leaked TPU pods, so write it deliberately.
- **The 118 MB context goes over the wire** to a GCS staging bucket on every build, rather than
  staying on a local disk. Seconds, but it is a new dependency and a new place for auth to fail.
- **Timeouts.** Cloud Build's default is 10 minutes and the tpu6e/tpu7x base images median 9.7–9.8.
  Set `--timeout` explicitly at 40m or these fail on day one.
- **Machine type must be set.** The default pool's `e2-standard-2` will not build these in any
  reasonable time; `--machine-type=e2-highcpu-32`, then measure. This is the number that decides
  whether the cost lands at $1,900 or $5,700.
- **IAM.** Per the standing rule, grant the Cloud Build SA `artifactregistry.writer` on the specific
  repositories, and grant the agent's Workload Identity SA `cloudbuild.builds.editor` — not
  project-wide.

Still worth ten minutes at some point, though it no longer blocks anything: deploy one rootless
BuildKit pod with `seccompProfile: Unconfined` and see whether AppArmor stops it. If it does not,
in-cluster builds become available with no privileged surface, and the trade above is worth
revisiting.

---

## 9. Workstream 6 — v7x, without spare chips ⬜

This is the part with no slack, so it gets the most structure.

### 9.1 v6e first, because it is free

Everything in the kube path except three v7x-specific facts can be proven on v6e at zero chip cost,
using the 26 spare chips:

```hcl
v6e-1-1x1 = { chips_per_node = 1, min_nodes = 0, nominal_nodes = 10, max_nodes = 10 }  # 10 chips
v6e-8-2x4 = { chips_per_node = 8, min_nodes = 0, nominal_nodes =  2, max_nodes =  2 }  # 16 chips
                                                                            # total = 26
```

`min_nodes = 0` throughout: this lane is a nightly, so paying one ~110 s node scale-up per step is
the correct trade against holding 26 chips idle for 23 hours a day. (The PoC ran `min_nodes = 2` on
the 1-chip lane because it was serving per-push traffic. Do not carry that over yet.)

The three facts v6e cannot prove: that a `tpu7x-standard-4t` node pool comes up from *our* v7x
reservation, that the tpu7x driver and image work on GKE nodes, and that the topology labels match
what the launcher generates. Note that `tpu-v7x-cluster-c` already demonstrates the first of these
— it is a GKE cluster holding `tpu7x-standard-4t` nodes with `SPECIFIC_RESERVATION` affinity on
reservation `cloudtpu-20251114223000-2002888989`, and it has been up since April.

### 9.2 The disagg benchmark is the v7x pilot

`tpu-v7x-cluster-c` holds 8 chips of the CI reservation in two fixed, non-autoscaling pools, and it
is not idle — it is the cluster the P/D disaggregation benchmark runs on, from
`tpu-inference/.buildkite/scripts/daily_run_gke_disagg.sh` and
`vllm-torchtpu/.buildkite/pipeline_disagg_gke.yml`. Converting *that pipeline* to the managed kube
lane is the v7x pilot: it costs the fleet nothing, and the 8 chips come under Kueue on the way
through.

It is the best first candidate on the board, for five reasons.

**It is already a Kubernetes workload.** No `docker run` → pod port, which is the expensive and
error-prone part of every other conversion. The manifests exist —
`.buildkite/kubernetes/manifests/v7x/{single_prefill,single_decode,proxy1p1d}.yaml` and
`storageclass.yaml`, already on `main` in both repos. The work is to submit them through the
launcher instead of `kubectl apply`, and to let Kueue own placement instead of two hand-pinned node
pools.

**It is a benchmark, not a gate.** Nothing merges on it. A bad night costs a data point.

**It proves the v7x-specific facts, on v7x.** Reservation affinity on a `tpu7x-standard-4t` pool,
tpu7x drivers on GKE nodes, topology labels — the three things §9.1 says v6e cannot prove. And it
proves them on a multi-pod workload (prefill + decode + proxy), which is a JobSet, which is the
mechanism the multi-host shapes in §9.3 need last.

**It demonstrates the ratchet's thesis on day one.** Today those 8 chips are pinned to two pools
with no autoscaling, so they are held 24/7 for a benchmark that runs once a day. Under Kueue at
`min_nodes = 0` they return to the reservation between runs. That is the whole argument of §9.3 —
"a converted lane gives its chips back at idle" — made concrete, measurable, and available as
headroom for the very next batch before a single agent is touched.

**It fixes three live problems as a side effect:**

| Today | After |
|---|---|
| Driven from a `queue: cpu` agent that holds one of 8 CPU slots for up to 630 minutes | a launcher pod on the manager cluster |
| `trap cleanup_1p1d EXIT` — if the agent VM dies the trap never fires and the TPU pods leak, stranding the chips. This is *the* reason for the "no CPU-fleet disruption 00:00–06:30 UTC" rule | launcher `ownerReference` plus SIGTERM upload-and-delete; the rule dissolves |
| Authenticates with `gcloud container clusters get-credentials` as the default compute SA, using its project-wide `container.developer` — one of the grants §6.3 wants to remove | workload identity, Connect Gateway, scoped to the profile |

It also deletes a small pile of incidental badness: the pipeline currently downloads `kubectl` and
the entire Google Cloud SDK tarball on every run because the agent image has neither.

**The launcher image is a stock Google one; do not build a `gcp-auth-plugin` image.** The PoC
referenced `us-central1-docker.pkg.dev/cloud-tpu-inference-test/mhhua-dev/gcp-auth-plugin:latest`, a
hand-rolled image in a personal repo in a project this lane no longer uses. Google publishes the
same contents: `gcr.io/google.com/cloudsdktool/google-cloud-cli:VERSION` installs
`google-cloud-cli-gke-gcloud-auth-plugin` pinned to the gcloud version, plus `kubectl`, and the
build fails if `kubectl version --client` does not run. Pin `:VERSION`, not `:latest`.

The catch is size: that tag is **1249 MB** compressed on amd64, because the same layer drags in
App Engine for four languages and five emulators. `:stable` is **116 MB** but is gcloud and `bq`
only — no plugin, no `kubectl`.

**Prefer the stock image with no Dockerfile at all.** A Dockerfile whose only content is `FROM`
plus a verification `RUN` still produces a derived image that has to be pushed to Artifact
Registry, which puts back both the AR grant and the build step this section removes. Reference
`gcr.io/google.com/cloudsdktool/google-cloud-cli:583.0.0` directly in the pod spec. The registry is
public. The launcher is long-lived and the image is cached on the node after the first pull, so the
1.2 GB is paid once per node, not once per run.

If that first pull does turn out to matter, extend `:stable` by multi-stage copy rather than by
apt. `:stable` is itself multi-stage and its runtime layer is a clean `debian:trixie-slim` holding
only `/usr/lib/google-cloud-sdk` — Google's apt source and keyring are in the discarded build
stage, and there is no `curl` or `gpg` left to re-add them, so `apt-get install` is not a one-liner
there. The plugin, though, is a **statically linked** 9.8 MB ELF, so it copies into any base:

```dockerfile
FROM gcr.io/google.com/cloudsdktool/google-cloud-cli:583.0.0 AS gcloud
FROM <launcher-base>
COPY --from=gcloud /usr/lib/google-cloud-sdk/bin/gke-gcloud-auth-plugin /usr/local/bin/
RUN gke-gcloud-auth-plugin --version
```

Copy the `/usr/lib` path; `/usr/bin/gke-gcloud-auth-plugin` is a symlink to it. That deb is amd64,
and this brings the plugin only — `kubectl` is a separate fetch.

**Sequencing.** This lands *after* §5 PR 7 (the launcher) and can run in parallel with §7's v6e dry
run — different chips, different repos, different failure modes. Convert `vllm-torchtpu` first: it
is a single step in one file, where `tpu-inference` shares the driver script with other work.
Keep the existing pipeline in place and soft-failing beside the new one for a fortnight, exactly as
in §7.4; the old path needs no chips of its own only once the new one owns the node pools, so plan
one short window where the benchmark does not run rather than trying to double up on 8 chips.

**Then, and only then**, `tpu-v7x-cluster-c` and its two hand-made pools get deleted and their 8
chips become a Kueue-managed pool declared in tfvars.

#### Rollback: the pools as they stand today

Deleting the two pools is the one step that cannot be undone by re-running Terraform, because
neither pool is in any state file — both were made by hand, and the reservation they draw on is
fully consumed, so a pool recreated with the wrong shape will not get its chips back. Recorded here
so a revert is a paste rather than an archaeology exercise. Read back from the live API on
2026-09-10; both pools were identical apart from their names.

| | |
|---|---|
| Cluster | `tpu-v7x-cluster-c`, zonal in `us-central1-c`, project `cloud-ullm-inference-ci-cd` |
| Pools | `tpu-v7x-np-0`, `tpu-v7x-np-2` — one node each, no autoscaling |
| Machine type | `tpu7x-standard-4t`; each node reports `google.com/tpu: 4`, 224 vCPU, 990725168Ki memory |
| Topology | `2x2x1`, placement policy `v7x221`, `goog-gke-tpu-node-pool-type: multi-host` |
| Reservation | `cloudtpu-20251114223000-2002888989`, `SPECIFIC_RESERVATION` on `compute.googleapis.com/reservation-name` |
| Boot disk | 100 GB `hyperdisk-balanced`, `COS_CONTAINERD`, node version 1.35.3-gke.1522000 |
| Identity | node service account `default` (the project default compute SA), `GKE_METADATA` workload metadata |
| Taint | `google.com/tpu=present:NO_SCHEDULE` |
| Other | `maxPodsPerNode: 110`, auto-repair and auto-upgrade on, integrity monitoring on, `disable-legacy-endpoints=true`, subnetwork `default` |

```bash
for np in tpu-v7x-np-0 tpu-v7x-np-2; do
  gcloud container node-pools create "$np" \
    --cluster tpu-v7x-cluster-c --zone us-central1-c \
    --project cloud-ullm-inference-ci-cd \
    --machine-type tpu7x-standard-4t --tpu-topology 2x2x1 --placement-type COMPACT \
    --num-nodes 1 --no-enable-autoscaling \
    --reservation-affinity specific --reservation cloudtpu-20251114223000-2002888989 \
    --disk-type hyperdisk-balanced --disk-size 100 \
    --workload-metadata GKE_METADATA --shielded-integrity-monitoring \
    --metadata disable-legacy-endpoints=true --max-pods-per-node 110
done
```

**Deleted 2026-09-10.** The reservation went from 128/128 in use to 120/128, so the eight chips are
back and `terraform apply` can boot the replacement pool. This is a cutover, not a side-by-side:
those were the only v7x chips not on a bare-metal agent, so `pipeline_disagg_gke.yml` has no
hardware from here until it is retired.

The concern this section was opened with — GKE called both pools **multi-host** and gave them a
placement policy, even though 4 chips at `2x2x1` is one `tpu7x-standard-4t` host, while `workers.tf`
emits `placement_policy` only when `is_multi_host`, which is false for this shape — resolves as *no
change needed*, on two pieces of evidence:

- The label the manifest actually depends on does not come from the placement policy. In
  `tpu-ci-us-east5`, `ct6e-standard-1t-1x1` is emitted by this Terraform with no placement policy
  (`1 > 1` is false) and its node carries `cloud.google.com/gke-tpu-topology: 1x1` with
  `goog-gke-tpu-node-pool-type` **empty**. GKE derives the topology label from the machine type on a
  single-host pool; multi-host is a classification that follows the placement policy, not a
  precondition for being labelled.
- The same holds on tpu7x specifically, not just on v6e. `nap-tpu7x-stand-4t-3igyb0rz` in
  `bodaborg-tpu7x-nap` is a `tpu7x-standard-4t` pool with no `tpuTopology` on it, and its instance
  template registers nodes with `cloud.google.com/gke-tpu-accelerator=tpu7x` and
  `cloud.google.com/gke-tpu-topology=2x2x1` and no `goog-gke-tpu-node-pool-type`. Those are the two
  labels the manifest selects on, so the shape needs no placement policy to be selectable. Read from
  the instance template rather than the nodes — that cluster's control plane is private and refuses
  connections from outside its authorized networks:

  ```bash
  gcloud compute instance-templates describe <template> --region us-central1 \
    --project cloud-tpu-shared-capacity --format="json(properties.metadata.items)"
  ```

The hand-made pools were multi-host only because they were created with an explicit
`--placement-type COMPACT`, which is in the recreate command above. So the honest predicate stays.

A related question this answers: **one pool, not two.** The two hand-made pools were two independent
single-host slices, which is what 1P1D wants — prefill and decode are separate processes exchanging
KV over the network, and a single 8-chip slice could not be split between two pods. But that is what
one pool scaling to two nodes already gives, since each node in a pool with no placement policy is
its own `2x2x1` slice. Two pools would only be needed for two *different* shapes, which the launcher
forbids anyway: one workload is admitted against one queue, and a queue is one shape. Watch the units
when reading any of this back — `tpu7x-8` in a VM or queue name is 8 TensorCores and therefore 4
chips, while `tpu7x-standard-4t` is 4 chips per VM, so 8 chips is *either* two `tpu7x-8`s or one
`tpu7x-16`, and only the first is what we have.

### 9.3 The chip-neutral ratchet

For each shape, convert in batches, holding total chips constant:

```
  ci-infra VM root:  instance_count  -= k
  k8s tfvars:        nominal_nodes   += k   (max_nodes = nominal_nodes, min_nodes = 0)
```

The two changes go in **one PR, applied VM-side first**, so the chips are free before the node pool
asks for them — the same discipline used for the v6e-8 / benchmark trade on 2026-08-28.

The ratchet works because of the idle asymmetry. A converted agent's chips return to the reservation
between jobs; a VM's never do. So after converting a fraction *f* of a shape at duty cycle *d*, the
average free chips are `f × C × (1 − d)` where *C* is the shape's total. That headroom absorbs the
*next* batch's burst, which is why batches can grow as you go rather than having to shrink.

Recommended order — lowest risk first, which is *not* the same as smallest:

| Order | Shape | Agents | Chips | Why here |
|---|---|---:|---:|---|
| 0 | disagg benchmark (§9.2) | — | 8 | already GKE, not a gate, costs the fleet nothing, and hands back 8 chips at idle |
| 1 | `tpu7x-2` | 16 | 16 | single-host, 1 chip/node, the most-proven path; 16 agents means batches of 4 are a 25% step |
| 2 | `tpu7x-8` | 18 | 72 | single-host (2×2×1), same path, the bulk of the capacity; batches of 3 |
| 3 | `tpu7x-16` | 2 | 16 | first multi-host slice; JobSet proven on v6e (build 272) and on v7x single-host by now |
| 4 | `tpu7x-32` | 1 | 16 | largest slice, and the agent is **not in Terraform** — converting it is also how it gets codified |

Two properties of this order worth stating: the two multi-host shapes are last, so the least-proven
mechanism runs against the most-proven infrastructure; and `tpu7x-32` — the one agent nobody manages
— stops being a special case as a side effect rather than as a separate project.

### 9.4 What would make this go wrong

- **Shape swaps are physical.** Node pools are single-shape, so reclaiming quota across shapes means
  draining and deleting nodes of one shape before the other's can be created: measured at ~300 s on
  top of the ~110 s scale-up. Set `max_nodes = nominal_nodes` during the migration so no lane can
  borrow, and revisit borrowing only once a shape is fully converted.
- **Two `tpu7x-8` node pools cannot become one `tpu7x-16` slice.** A multi-host pool is
  one pool, one slice, `max_nodes == hosts`, atomic scaling. Budget order-3 chips as new, not as
  recycled from order 2.
- **The dip is on one queue at a time.** Converting 4 of 16 `tpu7x-2` agents is a 25% dip on
  `tpu_v7x_2_queue` and 0% everywhere else. Announce per shape, not globally.

---

## 10. Everything else that makes this smooth

**Version pinning across MultiKueue.** The manager and every worker must agree on the Kueue version,
the JobSet version, the enabled `integrations.frameworks` list, and `quotaCheckStrategy`. MultiKueue
mirrors the workload object across clusters, so a version skew is a silent admission failure rather
than an error. Pin all three in tfvars and upgrade manager-and-workers in one PR, never separately.

**Node auto-upgrade will take the rug out.** A GKE auto-upgrade on a TPU pool mid-suite looks exactly
like a preemption. Put the TPU pools on a release channel with a maintenance exclusion that respects
the existing rule: **no disruption to CPU capacity between 00:00 and 06:30 UTC**, when the disagg
benchmark strands v7x TPUs if its agent dies.

**Observability changes character.** On bare metal, "my step is slow" means the test is slow. On
Kubernetes it can mean node scale-up (~110 s), image pull, admission wait, or preemption-and-resume.
Export from the manager: pending workloads per ClusterQueue, admission latency p50/p95, preemption
count, and node-pool scale-up duration. Put them beside the existing Buildkite metrics from
`ci_monitoring` so one dashboard answers "is CI slow, and whose fault is it".

**Image streaming and the cold path.** Cold-start on a fresh node is dominated by the image pull.
Image streaming is already on in the PoC's node pools; keep it, and keep the CI images in the
manager project's Artifact Registry so the cross-project reader grant is the only auth involved.

**Check quota before applying, not during.** The previous migration lost a week to a 9th v6e-8 that
hyperdisk quota could never create, leaving a phantom index that broke targeted applies. Before
PR 2, check hyperdisk and PD quota in `us-east5` against the node pool ceilings.

**The launcher treats the manifest as untrusted, and should keep doing so.** Kueue queue label must
match the profile, `serviceAccountName` must be one the cluster publishes, the image must come from
`allowed_image_repos`, and there must be a container named `workload`; PodSecurity `baseline` covers
the rest. `tpu-inference` builds fork pull requests, so the manifest genuinely is attacker-supplied.
Do not relax any of these to make a step easier to write.

**Cancellation has one correct path.** Cancel through Buildkite, never `kubectl delete job` — the
latter bypasses the controller's event watcher and leaves builds pending until step timeouts expire.
Worth putting in the pipeline README, not just the infra one.

---

## 11. Decisions needed

| | Question | Why it blocks | Default if no answer |
|---|---|---|---|
| ~~**D1**~~ | ~~Can `tpu-v7x-cluster-c` be reclaimed?~~ **Resolved 2026-09-08:** it is the disagg benchmark's cluster. Converting that pipeline to the kube lane is the v7x pilot — §9.2. | | |
| **D2** | Who are `630405687483-compute@` and `735972712744-compute@`, which hold `artifactregistry.admin`/`writer` and `storage.admin` on the CI project? | §6.2 — cannot remove a grant whose owner is unknown, cannot leave `artifactregistry.admin` for a project we do not recognise. | Leave, flag in the IAM PR. |
| **D3** | Is `cloud-ullm-inference-ci-cd` a CI-only project or a shared one? 9 humans hold `roles/editor`, 13 hold `compute.admin`, and there are unrelated buckets and clusters in it. | If shared, least privilege has a ceiling and CI should arguably move to its own project. That is a bigger change than this plan. | Treat as shared; scope §6 to CI-owned identities only. |
| ~~**D5**~~ | ~~Keep the static partition of the 26 v6e chips, or move to a custom compute class with `nodePoolAutoCreation`?~~ **Resolved 2026-09-09 (#524), against the compute class.** The partition was never the architecture, only the numbers: `max_nodes` set to each shape's share. Pools whose `max` oversubscribes the reservation compete for the same chips, and the reservation running out is the ceiling. The compute class also buys nothing at authoring time — a job names a Kueue queue and the shape reaches the pod through the launcher (D7) — and cannot hold a warm node, since auto-created pools are reaped to zero. Reconfirmed 2026-09-09 against `cloud-tpu-shared-capacity/bodaborg-tpu7x-nap`, the fleet cited as the compute-class precedent: it defines no custom compute class at all. It runs Node Auto Provisioning (`resourceLimits: tpu7x=307`), which materialises a pool per topology on demand and reaps it — no floor, so every job pays pool creation. | | |
| **D6** | Should `reservation_name` and `zone` become optional on a TPU node pool, allowing unreserved pools that may span zones? | Nothing needs it today, so adding it now repeats the `reservation_project` mistake: a knob that looks supported with nothing exercising it. Deferred 2026-09-09. Notes for when we revisit: dropping the reservation does not itself scatter a pool across zones (`node_locations` is an independent pin, currently set because the reservation is zonal), and "unreserved" composes with "multi-zone" only for single-host shapes, because COMPACT placement is zonal. A pool cannot draw on two reservations — `--reservation` is singular and reservations are single-zonal. | Keep both required. |
| ~~**D7**~~ | ~~Should the `gke-tpu-accelerator` / `gke-tpu-topology` nodeSelectors come from the ResourceFlavor or from the submitted workload?~~ **Resolved 2026-09-09: from the workload, via the launcher.** Flavor is Kueue's quota partition — quota is per flavor, cohort borrowing is per flavor, and Kueue skips a flavor whose `nodeLabels` conflict with what the podSet already asks for. A flavor per topology therefore makes it structurally impossible for a 1-chip job to use idle 8-chip capacity, whatever the numbers say. So the quota-bearing flavor carries the accelerator only, and `launch.py` substitutes `ACCELERATOR_LABEL`/`TOPOLOGY`/`CHIPS` from the profile, refusing a manifest that omits any of them (silent otherwise: the pod still requests chips, Kueue still admits it, and it lands on whatever pool has room). `bodaborg-tpu7x-nap` reaches the same split — its only quota-bearing TPU flavor is `tpu7x-flavor` (accelerator + reservation name, no topology); the fifteen per-topology flavors beside it are referenced by no ClusterQueue and several pin node pools that no longer exist. The consequence to live with: one shared flavor makes quota a chip count, not a placement guarantee, so a workload can be admitted while its shape is at `max_nodes` and then sit Pending on the scheduler rather than Suspended on the queue. | | |
| **D4** | Teardown timing — destroy the PoC now (§4), or keep the manager cluster running until PR 1 is applied? | The manager is in the right project and region and could be adopted. Rebuilding costs a day and proves reproducibility; adopting saves the day and proves nothing. | Destroy. Requirement 3 asks for reproducible. |

---

## Appendix A — PoC resource inventory

State: `gs://cloud-ullm-inference-ci-cd-tf-state/buildkite-tpu-ci/foundation/default.tfstate`
(serial 188, Terraform 1.15.5). 37 resource blocks, all destroyed by one `terraform destroy`.

**`cloud-ullm-inference-ci-cd`**

- `google_container_cluster.manager` — `tpu-ci-manager`, us-central1, 3 × `e2-standard-4`
- `google_container_node_pool.manager_system` — `system`
- `google_compute_router.manager` / `google_compute_router_nat.manager` — `tpu-ci-mgr-router`, `-nat`
- `google_service_account.manager_nodes` — `tpu-ci-mgr-node@`
- `google_project_iam_member.manager_nodes[×5]` — `artifactregistry.reader`, `logging.logWriter`,
  `monitoring.metricWriter`, `monitoring.viewer`, `stackdriver.resourceMetadata.writer`
- `google_project_iam_member.connect_gateway_kueue_wi[×2]` — `gkehub.gatewayEditor`, `gkehub.viewer`
  for `svc.id.goog[kueue-system/kueue-controller-manager]`
- `google_project_iam_member.connect_gateway_launcher_wi[×2]` — `gkehub.gatewayReader`,
  `gkehub.viewer` for `svc.id.goog[buildkite/tpu-launcher]`
- `google_project_iam_member.external_secrets_manager_secret_accessor`
- `google_project_iam_member.external_secrets_worker_secret_accessor["southamerica-west1-a"]`
- `google_project_iam_member.worker_nodes_manager_project["southamerica-west1-a"]` —
  `artifactregistry.reader` for `32478767326-compute@`
- `google_gke_hub_membership.worker["southamerica-west1-a"]` — `tpu-ci-southamerica-west1`
- `kubernetes_namespace_v1.buildkite_manager`; `helm_release.{buildkite_agent_stack, kueue_manager,
  jobset_manager}`; `null_resource.{kueue_manager_auth_plugin, manager_external_secrets_helm}`

**`cloud-tpu-inference-test`**

- `google_container_cluster.worker["southamerica-west1-a"]` — `tpu-ci-southamerica-west1-a`
- `google_container_node_pool.worker_system` — `system`, `e2-standard-4`, 1–3
- `google_container_node_pool.worker_tpu[".../v6e-1-1x1"]` — `ct6e-standard-1t`, min 2, max 10
- `google_container_node_pool.worker_tpu[".../v6e-8-2x4"]` — `ct6e-standard-8t`, max 1
- `google_compute_router.worker` / `google_compute_router_nat.worker`
- `google_storage_bucket.cache` — `tpu-ci-cache-southamerica-west1-c88355`
- `google_storage_bucket.models` — `tpu-ci-models-southamerica-west1-e33c7f`
- `google_storage_bucket_iam_member.{cache,models}_workload_identity_rw` — `storage.objectUser`
- `google_project_iam_member.{connect_gateway_kueue,connect_gateway_launcher,gkehub_service_agent,
  manager_nodes}_worker_project`
- `google_secret_manager_secret_iam_member.launcher_analytics_token` — on
  `tpu_commons_buildkite_analytics_token`
- `null_resource.{jobset_worker, kueue_worker, worker_external_secrets_helm}`

**Chips released:** 18, back to the shared 256-chip `southamerica-west1-a` reservation
`cloudtpu-20250327121505-861300654` (253/256 in use as of 2026-09-08).

---

## Appendix B — sequencing

Dependencies, not dates. `→` is "must finish first".

```
§6.3 agent SAs        ─────────────────────────────────►   (independent, do first)
§6.4 dead config      ─────────────────────────────────►   (independent)
§7.1 test fixes       ─────────────────────────────────►   (independent)

§4 teardown → §5 PR1 → PR2 → PR3 → PR4 → PR5 → PR6 → PR7 → PR8
                                                       │
                          ┌────────────────────────────┴────────────────────────┐
                          │                                                     │
            §7.2 dry-run lane (v6e, 26 chips)             §9.2 disagg → kube (v7x, 8 chips)
                          │                                                     │
                 14 green nights                                       benchmark stable
                          │                                                     │
                          └──────────────────┬──────────────────────────────────┘
                                             │
                                  §8 cpu → kube  ·  §9.3 v7x ratchet
```

The two branches under PR 7 are independent — different accelerators, different repos, different
failure modes — and should run in parallel. `§8` waits on `§9.2` (it removes the long-held CPU slot
and the 00:00–06:30 UTC constraint); `§9.3` waits on `§9.2` (it needs the v7x path proven) and
benefits from `§7.2` having shaken out the launcher on volume.
