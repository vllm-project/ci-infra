# Why the detector looks the way it does

Notes from the investigation that produced this skill (2026-09-02), kept so the
thresholds can be argued with rather than guessed at.

## Agent state is useless on its own

Every degraded agent found so far reported `connection_state: connected` and
`paused: false`. Neither the queues page nor `bk agent list` distinguished them
from healthy peers. Anything built on agent state alone will see a green fleet.

`bk agent list --output json` also drops the fields that matter (`job`,
`last_job_finished_at`, `connected_at`, `lost_at`), which is why this skill goes
to the REST API directly.

## Job history has to be reconstructed

There is no per-agent job history endpoint. The only route is
`/v2/organizations/tpu-commons/builds?created_from=...`, paged, grouping jobs by
`job.agent.name`. A live 24h window is roughly 10k jobs across ~79 agents.

Builds created before the window can hold jobs that finished inside it, so the
fetch reaches back `2 * window_hours` and filters on `finished_at`.

## disk-full: the counters cannot convict, only the log can

The first version of this detector keyed on idleness and found nothing, because
a disk-full agent is *busy*: it keeps winning jobs and failing them. Observed on
`tpu7x-8-ci-17`: 35 failures out of 97 jobs.

The second version keyed on "high failure rate + short median failure duration",
on the theory that these die during a docker pull in ~47s. That is true for
docker pulls and false for everything else — a model download can churn for many
minutes before it runs out of room, and a slow-failing disk-full agent is
exactly as damaging. Timing was dropped as a gate and kept only as reported
evidence.

What is left is: a loose statistical pre-filter picks who to investigate, then an
ENOSPC marker in an actual job log convicts. Everything else is reported as
`high-failure-rate` for a human, because flaky tests and a broken pipeline
produce identical counters and a VM replacement fixes neither.

`DISK_FULL_MARKERS` covers how different runtimes render ENOSPC: the kernel
string (`no space left on device`), Python's `[Errno 28]` (huggingface_hub model
downloads), hf_transfer's `not enough free disk space`, and docker's
`failed to register layer`, which sometimes elides the errno entirely.

Only the newest `MAX_LOGS_PER_AGENT` failures are read — the oldest failure in a
window may predate the disk filling up.

## silent-wedge: only meaningful relative to the queue

`vllm-ci-cpu-64-core-6` ran 0 jobs in a 40.9h window while its 7 peers ran
12-56. It was `connected`, unpaused, and the VM had been RUNNING for 8 days. A
recreate fixed it.

Two false positives shaped the rule:

- Six `cpu` agents idle for 7.7h all had `exit_status: 0` in a synchronised
  burst. The queue was simply quiet. Hence scoring per queue and skipping any
  queue with no work at all.
- `v6e-1-ci-1` had reconnected 1.25h earlier. Hence `WEDGE_MIN_HOURS`.

## The attached disk is not disposable

`ci_v6e`, `ci_v7x`, `ci_v5`, `ci_v6` and `benchmark` attach a separate
`google_compute_disk`, mounted at `/mnt/disks/persist`, which CI jobs
bind-mount. It is not `auto_delete`, and the startup script only formats it
`if ! blkid`. Replacing just the instance therefore reattaches the same full
disk and the agent comes straight back degraded.

`ci_cpu` and `ci_cpu_64_core` differ: the boot disk is inline with
`auto_delete = true`, so an instance replace already gives a fresh disk and `-d`
is a no-op. Passing `-d` unconditionally for disk-full is safe across both.

## SSH is not a diagnostic here

SSH to these VMs fails with "Connection timed out during banner exchange" on
*healthy* hosts too — an IAP/firewall gap, not a symptom. Always test a healthy
peer before reading an SSH hang as evidence. Confirm from job logs instead.

## Running on Linux

The fleet `.terraform.lock.hcl` files originally recorded `h1:` hashes for one
platform only, so `terraform state pull` failed on linux_amd64 with "Required
plugins are not installed". Fixed by recording hashes for linux_amd64,
darwin_arm64 and darwin_amd64:

```bash
terraform -chdir=<fleet-dir> providers lock \
  -platform=linux_amd64 -platform=darwin_arm64 -platform=darwin_amd64
```

Re-run that when a provider version changes, or Linux users are locked out
again.
