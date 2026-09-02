# Driving this from `/loop`

Run `/loop` from the repo root. Three prompts, in increasing order of autonomy —
start at the top and move down once you trust it.

## 1. Report only (start here)

Nothing is ever changed. Good for a first day of watching it, and for confirming
the thresholds match what you would have judged by eye.

```
/loop Use the agent-health skill to scan the tpu-commons Buildkite fleet.
Run: uv run python scripts/agent_health.py scan --json
from terraform/gcp_old/tpu-inference/skills/agent-health

If degraded is empty and blocked_queues is empty, reply with exactly
"clean: N agents, no action" and nothing else.

Otherwise, for each entry in degraded print the agent name, queue, reason and
evidence, and the exact recreate command you would run. Do not run it. Also
list anything in needs_review in one line each.
```

## 2. Auto-fix disk-full, ask about the rest (recommended)

Disk-full is the mode worth acting on unattended: it is log-confirmed, it is
actively failing PRs, and the fix is deterministic. A silent wedge costs
capacity but breaks nothing, so it can wait for a human.

```
/loop Use the agent-health skill to keep the tpu-commons Buildkite fleet healthy.
Work from terraform/gcp_old/tpu-inference/skills/agent-health

1. Run: uv run python scripts/agent_health.py scan --json
2. If blocked_queues is non-empty, do not recreate anything. Report the
   blocked queues and stop — that shape means a fleet-wide fault, not a
   per-VM problem.
3. For each degraded entry with reason "disk-full": run
   uv run python scripts/agent_health.py recreate <agent>
   one agent at a time, waiting for each to finish before starting the next.
   Do not check whether it holds a job — a disk-full agent fails whatever it
   is running anyway.
4. For each degraded entry with reason "silent-wedge": report it with its
   evidence and the recreate command, but do not run it.
5. Ignore needs_review entries unless the same agent appears three ticks in a
   row, in which case mention it once.

Keep each tick's output short. If nothing was degraded, reply with exactly
"clean: N agents, no action" and nothing else.
```

## 3. Fully autonomous

Same as (2) but step 4 becomes:

```
4. For each degraded entry with reason "silent-wedge": run
   uv run python scripts/agent_health.py recreate <agent>
   The script already refuses if the agent holds a job; do not override it.
```

## Notes on pacing

Let `/loop` pace itself rather than pinning an interval. A scan is ~15-30s and
costs a few hundred API calls, so 20-30 minutes between ticks is plenty — a disk
fills over hours, not seconds. Tighten to ~5 minutes only while actively
watching a recreate land.

A recreate takes several minutes for a CPU VM and longer for a TPU VM. The next
tick will naturally see the new agent as healthy (freshly connected agents are
exempt from the wedge rule for `WEDGE_MIN_HOURS`), so there is no risk of the
loop recreating the same host twice in a row.

## What the loop must not do

- Do not bypass `blocked_queues`. It exists precisely for the case where the
  loop would otherwise recreate half a fleet in response to a bad pipeline.
- Do not pass `--force`. Every guard it skips is one the loop is not qualified
  to judge.
- Do not act on `needs_review` / `high-failure-rate`. No ENOSPC marker was found
  in the logs, so a VM replacement is a guess.
- Do not SSH into anything to confirm. SSH is blocked fleet-wide and fails on
  healthy hosts too.
