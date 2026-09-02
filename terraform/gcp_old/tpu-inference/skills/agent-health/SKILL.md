---
name: agent-health
description: Find degraded Buildkite agents in the tpu-commons cluster (disk full, silently wedged) and recreate their VMs with rolling_restart.py. Use when asked to check CI agent health, investigate stuck or failing agents, or run a monitoring loop over the fleet.
---

# TPU inference CI agent health

Degraded agents in this cluster still report `connection_state: connected` and
`paused: false`. The Buildkite queues page looks fine while they are eating
jobs, so this skill exists to tell the difference from job history rather than
from agent state.

## The two failure modes

**disk-full** — the agent keeps accepting jobs and fails them on ENOSPC. It
never looks idle, so any idleness-based check misses it. The disk can fill
during a docker pull, a model download, a pip install or a build cache write,
and those fail on wildly different timescales (seconds to many minutes), so
detection keys on the ENOSPC marker in the job log, never on failure timing.

**silent-wedge** — the agent stops accepting work while its queue peers cycle
normally. No failures, just a quiet capacity loss.

A high failure rate with no ENOSPC marker is reported as `high-failure-rate`
and never auto-remediated: flaky tests and broken pipelines look identical on
the counters, and recreating a VM fixes neither.

## Discovery

The repo `.gitignore` excludes `.claude/`, so the source lives here, tracked and
reviewable. To have Claude Code pick it up as a skill, link it in locally — the
symlink is ignored, the code is not:

```bash
mkdir -p terraform/gcp_old/tpu-inference/.claude/skills
ln -s ../../skills/agent-health \
  terraform/gcp_old/tpu-inference/.claude/skills/agent-health
```

Nothing depends on that link. Every command below, and every prompt in
`references/loop-prompt.md`, invokes the script by path and works without it.

## Check status

```bash
cd terraform/gcp_old/tpu-inference/skills/agent-health
uv sync --all-groups                      # first time only
uv run python scripts/agent_health.py scan
uv run python scripts/agent_health.py scan --json     # for a loop to parse
```

Read-only. Takes ~15-30s: it walks recent builds because Buildkite has no
per-agent job history endpoint, then reads a few job logs for any suspect.

`--window-hours` (default 24) sets the history window. Shorten it for a
fast-cycling loop, lengthen it to catch a slow-burning fleet.

Output splits into `degraded` (actionable) and `needs_review` (reported only).

## Run safely

```bash
# what would happen
uv run python scripts/agent_health.py recreate <agent> --dry-run
# do it
uv run python scripts/agent_health.py recreate <agent>
```

`recreate` re-runs the scan itself and refuses to act unless the agent is
actionable *right now*, so a stale scan cannot trigger a replacement.

Guard rails, all bypassable with `--force`:

- **Not actionable** — the agent must currently classify as disk-full or
  silent-wedge.
- **Blast radius** — if more than 25% of a queue is degraded (and more than one
  agent), it stops. That shape means a fleet-wide or pipeline fault, and
  recreating VMs one at a time would churn the fleet without fixing anything.
  A single bad agent never trips this, whatever the queue size.
- **In-flight job** — blocks a silent-wedge recreate only. A disk-full agent
  fails whatever it is holding anyway, so waiting protects nothing and just
  prolongs the outage.

### The disk matters

The TPU fleets attach a **separate** `google_compute_disk` at
`/mnt/disks/persist` for models and caches. It is not `auto_delete`, and the
startup script only formats it `if ! blkid`. An instance-only replace hands the
new VM back the same full disk and the agent returns still degraded.

So `recreate` passes `-d/--replace-disks` for disk-full and withholds it for
silent-wedge (whose disk is fine, and reformatting would throw away every
cached model). Override with `--replace-disks` / `--no-replace-disks`.

## How it maps an agent to Terraform

`resolve` prints the address without touching anything:

```bash
uv run python scripts/agent_health.py resolve <agent>
```

It runs `terraform state pull` across each fleet directory and matches the VM
by `attributes.name`, relying on the fleets' name SSOT (agent, VM and disk share
one name). Legacy fleets predate that, so it also tries the agent's `hostname`.
Reading state rather than hardcoding a name regex means new fleets work without
editing the script.

Remediation always shells out to the existing
`terraform/gcp_old/tpu-inference/scripts/rolling_restart.py`, so the actual
change stays a reviewable `terraform apply -replace -target`.

## Auth

`BUILDKITE_API_TOKEN` if set, otherwise `bk auth token`. It deliberately does
not read Secret Manager — it runs under whoever is driving it.

`terraform state pull` needs application-default credentials for the GCS
backend and an initialised working directory (`terraform init`) in each fleet
dir it searches.

## Change the automation

Thresholds are module-level constants at the top of `scripts/agent_health.py`,
each with a comment on why it is set where it is. The pre-filter constants
(`MIN_JOBS_FOR_RATE`, `MIN_FAILURES_FOR_LOG_CHECK`, `FAILURE_RATE_THRESHOLD`)
only decide whose logs get read — loosen them freely, since the ENOSPC marker
is what actually convicts. `DISK_FULL_MARKERS` is where to add a new phrasing
when some tool renders ENOSPC differently.

```bash
uv run ruff check scripts && uv run ruff format --check scripts && uv run pytest
```

See `references/loop-prompt.md` for driving this from `/loop`, and
`references/detection.md` for the evidence behind the thresholds.
