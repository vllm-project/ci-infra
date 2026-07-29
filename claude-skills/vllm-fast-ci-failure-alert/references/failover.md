# Host failover

## Prerequisites

- Linux with systemd
- `uv`
- Databricks SQL warehouse credentials with read access to the vLLM warehouse
- A Slack bot token that can post to the configured channel

Keep credentials in a root-readable environment file outside the clone. Never
commit the populated file.

## Planned cutover

Stop the old timer before copying SQLite so no alert can be reserved while the
file is in transit:

```bash
sudo systemctl disable --now vllm-fast-ci-failure-alert.timer
sudo tar -C /home/ubuntu/vllm-fast-ci-failure-alert \
  -czf /tmp/vllm-fast-ci-failure-alert-state.tgz state.sqlite3
```

Fence the old host so it cannot later boot with its timer active.

On the replacement host:

```bash
git clone https://github.com/vllm-project/ci-infra.git
cd ci-infra/claude-skills/vllm-fast-ci-failure-alert
sudo scripts/install.sh \
  --env-file /path/to/vllm-fast-ci-failure-alert.env \
  --no-start
sudo tar -C /var/lib/vllm-fast-ci-failure-alert \
  -xzf /path/to/vllm-fast-ci-failure-alert-state.tgz
sudo chown "$(id -un)":"$(id -gn)" \
  /var/lib/vllm-fast-ci-failure-alert/state.sqlite3
sudo chmod 0600 /var/lib/vllm-fast-ci-failure-alert/state.sqlite3
```

Run the dry run from `SKILL.md`. If it succeeds:

```bash
sudo systemctl enable --now vllm-fast-ci-failure-alert.timer
```

## Unplanned failover

Install with `--no-start` and fence the old host before enabling the
replacement. SQLite is the deduplication authority; two active hosts can post
the same job.

If the old state file is unavailable, the replacement can repost matching jobs
from the current lookback window. Choose one:

- wait 30 minutes after fencing the old host before enabling the timer;
- accept at most one duplicate per matching job in the lookback window.

Waiting avoids duplicates but can miss fast failures that finish during the
gap. The timer uses `Persistent=true`, so `enable --now` may immediately run a
missed schedule.

## Persistent state

The default state file is
`/var/lib/vllm-fast-ci-failure-alert/state.sqlite3`. It records:

- Buildkite job ID, the primary deduplication key;
- job finish and reservation timestamps;
- Slack message timestamp after successful delivery.

Successful rows are retained for 90 days. Stale unsent reservations expire
after 10 minutes. Logs remain in the systemd journal.
