# Working on the kube CI fleet

For new team members and coding agents. This file covers how to work on the
fleet without breaking it. The [README](README.md) explains the design and why
it is that way; read its "Running a step", "How the launcher works" and
"Debugging a step" sections before changing the launcher or chasing a failure.

## What runs where

- A Buildkite step on queue `kube` gets an agent pod on the manager cluster,
  `tpu-ci-manager` (us-central1). The pod's `launcher` container runs
  `kueue/launcher/launch.py` for as long as the step lasts.
- The launcher renders a Job or JobSet, submits it to Kueue on the manager and
  watches it until it ends. Kueue admits it against fleet quota, and
  MultiKueue copies it to the worker that owns the shape: `tpu-ci-us-east5`
  for v6e, `tpu-ci-us-central1` for v7x. The TPU pods run on the worker.
- Which steps go to kube is decided in the test repos, not here: `CI_FLEET`
  and `.buildkite/kube_rollout.conf` in vllm-torchtpu and tpu-inference. Their
  steps call the launcher through `.buildkite/kubernetes/run.sh` and
  `run_multihost.sh`, and an environment variable reaches the pod only if
  `.buildkite/kubernetes/common.sh` lists it in `FORWARD`.
- Shapes, quotas, Spot pools, priorities and launcher timeouts are in
  `prod.auto.tfvars`. Read current numbers there; numbers quoted anywhere else,
  this file included, go stale.

## Access

```bash
gcloud container clusters get-credentials tpu-ci-manager --location us-central1 --project cloud-ullm-inference-ci-cd
gcloud container clusters get-credentials tpu-ci-us-central1 --location us-central1 --project cloud-ullm-inference-ci-cd
gcloud container clusters get-credentials tpu-ci-us-east5 --location us-east5 --project cloud-ullm-inference-ci-cd
```

The contexts are `gke_cloud-ullm-inference-ci-cd_<region>_<cluster>`. Do not
pass `--dns-endpoint`: the DNS endpoint it writes answers 403. Buildkite is the
`bk` CLI against org `vllm`. Every cluster's container logs and Kubernetes
events are in Cloud Logging in `cloud-ullm-inference-ci-cd`, and they outlive
the pods, which is usually where a finished or cancelled step has to be read.

## Where to change what

| To change | Edit | Then |
| --- | --- | --- |
| Clusters, node pools, buckets, IAM | `*.tf`, `prod.auto.tfvars` | `terraform plan`, `terraform apply` |
| Queues, quota, flavors, priorities, launcher timeouts | `prod.auto.tfvars`, `kueue/templates/`, `scripts/generate_manifests.py` | `./scripts/generate_manifests.py`, commit `kueue/generated/`, deploy |
| What the launcher does | `kueue/launcher/launch.py` | deploy; it ships as the `tpu-launcher-scripts` ConfigMap |
| What every workload pod gets | `kueue/launcher/pod_defaults.yaml` | deploy |
| The launcher image (Cloud CLI version) | `kueue/launcher/Dockerfile` | README "Rebuild the launcher image" |
| The dashboard | `dashboard/` | README "Rebuild the dashboard" |

A new shape touches both rows one and two: Terraform makes the node pool, and
the deploy makes its flavor, queue and registry entry. Node pools go first.

## Before you push

There is no test suite. At least:

- `python3 -m py_compile kueue/launcher/launch.py` (and the dashboard's files
  if you touched them).
- `./scripts/generate_manifests.py` leaves `kueue/generated/` with no diff, or
  the diff is committed with your change. Template comments count: they are
  rendered into the ConfigMaps.
- `./scripts/deploy_manifests.py --mode diff` shows only what you meant to
  change.
- For a launcher helper that parses cluster output, run it against a real
  object or event from the cluster, not only a made-up one.
- For the dashboard, run it locally against the live fleet (README "Rebuild
  the dashboard") and open the pages you changed.

## Deploying

The fleet runs whatever was last applied, which is not always main. Changes
are applied from the open PR and merged once they are live and checked:

1. Open the PR.
2. Build a tree from current `origin/main`, cherry-pick the PR's commits, and
   run `./scripts/generate_manifests.py` and `./scripts/deploy_manifests.py`
   from it. If another PR is applied but not merged yet, cherry-pick it too, or
   this deploy undoes it. Say in the PR which unmerged PRs were on the fleet.
3. Check the change on the fleet (below).
4. Merge.

Every deploy restarts the Kueue controller on all three clusters, which
refuses pod creation for a few seconds (README "A deploy briefly rejects pod
creation"). Running workloads are not affected.

**A launcher change reaches only steps that start after the deploy.** A
running launcher keeps the code and the registry values it started with. To
check a launcher change, find when the ConfigMap changed and read a launcher
that started after that:

```bash
kubectl -n buildkite get configmap tpu-launcher-scripts \
  -o jsonpath='{range .metadata.managedFields[*]}{.manager} {.time}{"\n"}{end}'
kubectl -n buildkite get pods --sort-by=.metadata.creationTimestamp | tail
kubectl -n buildkite logs <agent pod> -c launcher | grep -E '~~~ launcher|Traceback'
```

The same holds for registry values such as `spot_queues` and the `tpu_*`
timeouts, which is why removing Spot from a queue takes two deploys: drop the
queue from `spot_queues`, wait about 25 minutes for running launchers to pass
their Spot offer, then remove the flavor. A launcher that offers Spot to a
queue with no Spot flavor leaves its step unadmitted until its queue budget
runs out.

## Rules

- Never edit `kueue/generated/` by hand.
- Removing an object from `kueue/generated/` does not remove it from the
  clusters. Delete it by hand on the manager and on every worker that had it.
- Do not change a running workload by hand (`kubectl patch`, deleting its
  pods or Workload) to unstick one step. Cancel or retry the step in
  Buildkite. Do not retry or cancel other people's PR builds; tell the owner
  what you found.
- Do not print secret values. Rotation is in the README runbook.
- Do not empty or recreate the cache buckets: a cold cache costs about 2.7x a
  suite's chips to refill.
- A multi-host node pool is exactly one slice. More slices of a shape means
  more pools (`slices` in `prod.auto.tfvars`), not a larger `max_nodes`.
- Before moving the clusters to a new GKE minor version, check that a pod with
  a gcsfuse PVC still scales a TPU pool from zero. On a 1.36.4 test cluster it
  did not (10-14): the autoscaler refused every TPU pool and logged it as
  `NodeAffinity`.

## Commits and PRs

- Titles start with `tpu-inference/k8s:`, or `tpu-inference/k8s/dashboard:`
  for the dashboard. Sign off every commit (`git commit -s`, DCO).
- The reasoning goes in the commit message. Keep the PR description short.
- Link Buildkite builds by full URL: in a PR body a bare `#616` links to PR 616.
- Code comments say why the code is the way it is. Rejected alternatives and
  how it got there belong in the commit message.
- Never push to `main`. If you use git-spice, create branches with no upstream
  (`git worktree add --no-track`, `git branch --no-track`): `gs branch submit`
  pushes a branch that tracks `origin/main` to `main`.
- The README is the design record. When you change behaviour, change the
  paragraph that describes it in the same PR.
