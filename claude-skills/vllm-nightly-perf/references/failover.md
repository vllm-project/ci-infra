# Host failover

## Prerequisites

- Linux with systemd
- Python 3.11 or newer
- `uv`
- A Buildkite token with `read_builds` and `write_builds`
- The production Slack incoming webhook

Keep credentials in a root-readable environment file outside the clone. Never
commit the populated file.

## Planned cutover

If the old host already uses this bundle:

```bash
sudo systemctl disable --now vllm-nightly-perf-trigger.timer
sudo systemctl disable --now vllm-nightly-perf-report.timer
sudo tar -C /var/lib -czf /tmp/vllm-nightly-perf-state.tgz vllm-nightly-perf
```

Fence the old host so it cannot later boot with its timers active.

If the old host still uses the shared `claude-cron` deployment, set
`enabled: false` in both task definitions and verify the scheduler reload:

- `tasks/vllm-nightly-perf-trigger.yaml`
- `tasks/vllm-nightly-perf-report.yaml`

Verify with `claude-cron list` and the scheduler log before enabling the new
host.

Do not stop the shared scheduler if it also owns other automations.

On the replacement host:

```bash
git clone https://github.com/vllm-project/ci-infra.git
cd ci-infra/claude-skills/vllm-nightly-perf
sudo scripts/install.sh \
  --env-file /path/to/vllm-nightly-perf.env \
  --no-start
sudo tar -C /var/lib -xzf /path/to/vllm-nightly-perf-state.tgz
sudo chown -R "$USER":"$(id -gn)" /var/lib/vllm-nightly-perf
```

Run both dry runs from `SKILL.md`. If they succeed:

```bash
sudo systemctl enable --now vllm-nightly-perf-trigger.timer
sudo systemctl enable --now vllm-nightly-perf-report.timer
```

## Unplanned failover

If the old host is unavailable, install with `--no-start`. The trigger checks
Buildkite before creating a build, but that lookup is not a distributed lock:
two hosts can race. Fence the old host before enabling the replacement.

The Slack report state is local. Without the old
`vllm_perf_eval_report_state.json`, the replacement may post the latest report
once more. Run the report dry run, verify whether its exact Buildkite build was
already posted in `#ci-notifications`, and then either let it post or use:

```bash
.venv/bin/vllm-nightly-perf adopt \
  --state-dir /var/lib/vllm-nightly-perf \
  --build-number BUILD_NUMBER
```

Only adopt a build after verifying its existing Slack post. The timers use
`Persistent=true`, so `enable --now` may immediately execute missed schedules.

## Persistent state

The default state directory is `/var/lib/vllm-nightly-perf`. Back up:

- `vllm_perf_eval_report_state.json`: last reported Buildkite builds
- `vllm_perf_eval_trigger_result.json`: latest created build response
- `vllm_perf_eval_report_payload.json`: latest rendered Slack payload
- `vllm_perf_eval_report_rows.json`: latest calculated rows

Logs remain in the systemd journal.
