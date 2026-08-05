# CI Automations and Alerts

This directory contains portable scheduled automations and interactive CI
investigation skills. A portable automation includes its runtime, locked
dependencies, systemd unit templates, installer, environment contract, tests,
and failover procedure.

Merging a skill does not move a live service automatically. Cut over each
automation explicitly, transfer its state, and ensure only one host has its
timer enabled.

## Portable scheduled automations

| Automation | Purpose | Schedule | External action | systemd unit |
| --- | --- | --- | --- | --- |
| Nightly perf trigger | Start `vllm/perf-eval` from the newest scheduled `release-v2` build whose x86_64 CUDA 13.0 image job passed | Daily at 23:00 America/Los_Angeles | Create a Buildkite `perf-eval` build | `vllm-nightly-perf-trigger.{service,timer}` |
| Nightly perf report | Compare H200 throughput/GPU, p99 TTFT latency, and eval accuracy against rolling history | Daily at 09:00 and 19:00 America/Los_Angeles | Post to `#ci-notifications` (`C0ABTNM9L5U`) | `vllm-nightly-perf-report.{service,timer}` |
| Fast CI failure alert | Find Databricks-ingested CI script jobs that failed in 30 seconds or less during the previous 30 minutes | Every 15 minutes at `:03`, `:18`, `:33`, and `:48` | Post to `#ci-alert` (`C0ANHBE642Y`) | `vllm-fast-ci-failure-alert.{service,timer}` |

### Nightly perf trigger and report

Skill: [`vllm-nightly-perf/`](vllm-nightly-perf/)

The trigger accepts a scheduled release even when the aggregate `release-v2`
build failed. Its exact eligibility gate is a passed
`Build release image - x86_64 - CUDA 13.0` job. It deduplicates against
existing Buildkite nightlies on a best-effort basis.

The report uses the vLLM CI dashboard's nightly data, calculates a seven-day
average and standard deviation plus a 30-day peak, and records posted
Buildkite build numbers locally.

Required credentials:

- `BUILDKITE_API_TOKEN` with build read/write access;
- `SLACK_WEBHOOK_URL` or `VLLM_CI_SLACK_URL`.

Persistent state defaults to `/var/lib/vllm-nightly-perf`. Read
[`vllm-nightly-perf/references/failover.md`](vllm-nightly-perf/references/failover.md)
before installation or migration.

### Fast CI failure alert

Skill:
[`vllm-fast-ci-failure-alert/`](vllm-fast-ci-failure-alert/)

The alert queries `vllm_data_warehouse.buildkite` through a Databricks SQL
warehouse. It selects failed, failing, broken, or timed-out script jobs in the
`CI` pipeline whose measured runtime is between zero and the configured
threshold. Slack messages contain at most eight jobs.

SQLite reserves each Buildkite job ID before delivery, records its Slack
message timestamp, retries stale unsent reservations after 10 minutes, and
retains successful rows for 90 days.

Required credentials and configuration:

- `DATABRICKS_HOST`;
- `DATABRICKS_TOKEN`;
- `DATABRICKS_WAREHOUSE_ID`;
- `SLACK_BOT_TOKEN`;
- `SLACK_CHANNEL_ID`;
- optional `LOOKBACK_MINUTES` and `MAX_DURATION_SECONDS`.

Persistent state defaults to
`/var/lib/vllm-fast-ci-failure-alert/state.sqlite3`. Read
[`vllm-fast-ci-failure-alert/references/failover.md`](vllm-fast-ci-failure-alert/references/failover.md)
before installation or migration.

## Install and cut over

From the selected skill directory:

```bash
cp env.example /path/outside/the/repository/automation.env
# Populate the environment file without committing it.
sudo scripts/install.sh \
  --env-file /path/outside/the/repository/automation.env \
  --no-start
```

Then:

1. stop and disable the old timer;
2. fence the old host so it cannot restart the timer;
3. copy the state described by the skill's failover document;
4. run the documented dry run;
5. enable the new timer with `sudo systemctl enable --now TIMER_NAME`.

Use `--no-start` until the cutover is ready. The timers use
`Persistent=true`, so enabling them can immediately execute a missed
schedule.

## Operate

List all portable automation timers:

```bash
systemctl list-timers 'vllm-nightly-perf-*' \
  vllm-fast-ci-failure-alert.timer
```

Inspect recent results:

```bash
journalctl -u vllm-nightly-perf-trigger.service -n 100 --no-pager
journalctl -u vllm-nightly-perf-report.service -n 100 --no-pager
journalctl -u vllm-fast-ci-failure-alert.service -n 100 --no-pager
```

The fast-alert service is a oneshot unit. `inactive (dead)` between runs is
normal when its timer is `active (waiting)`.

Dry runs may query external read APIs and write diagnostic or SQLite files,
but they do not create Buildkite builds or post to Slack. Live trigger,
report, and Slack-test commands require explicit approval because they cause
external writes.

## Current live deployment

At the time this catalog was added, `dev` still runs the original host-local
deployments:

| Automation | Live location | Status |
| --- | --- | --- |
| Nightly perf trigger/report | `/home/ubuntu/vllm-ci-report` under the shared `claude-cron` service | Portable skill available; production cutover not performed |
| Fast CI failure alert | `/home/ubuntu/vllm-fast-ci-failure-alert` | Portable skill available; production cutover not performed |
| Full CI results report | `/home/ubuntu/vllm-ci-report/tasks/vllm-ci-report.yaml` | Host-local only; no portable skill yet |

The full CI results report runs around 05:00 and 19:00
America/Los_Angeles and posts to `#ci-notifications`. Do not assume it can be
recovered from this repository until it receives its own portable skill.

Verify live state before every migration; this table is an architectural
status record, not a replacement for `systemctl`, scheduler history, or
Buildkite checks.

## Interactive investigation skills

These files guide an agent but do not install timers or send scheduled alerts:

- [`bisect-nightly.md`](bisect-nightly.md): bisect failures in full nightly or
  daily CI runs.
- [`pytorch-bump-triage.md`](pytorch-bump-triage.md): distinguish PyTorch or
  Triton bump regressions from failures already present on main.

## Validate changes

From each portable skill directory:

```bash
uv sync --all-groups
uv run ruff check scripts
uv run ruff format --check scripts
uv run pytest
shellcheck scripts/install.sh
bash -n scripts/install.sh
```

Also run the skill validator and verify rendered units with
`systemd-analyze verify` on Linux.
