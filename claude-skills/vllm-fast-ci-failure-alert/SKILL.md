---
name: vllm-fast-ci-failure-alert
description: Install, migrate, operate, or diagnose the portable vLLM fast CI failure Slack alert. Use when moving the Databricks-backed short-job alert to another systemd Linux host, checking its 15-minute timer, previewing matching Buildkite CI jobs, testing Slack delivery with explicit approval, or investigating missing or duplicate alerts.
---

# vLLM Fast CI Failure Alert

Operate a deterministic alert that:

- queries the prior 30 minutes of vLLM Databricks CI job data;
- selects failed script jobs that ran for 30 seconds or less;
- posts batches of up to eight jobs to Slack;
- deduplicates Buildkite job IDs in SQLite for 90 days.

The timer runs every 15 minutes at `:03`, `:18`, `:33`, and `:48`. The job
does not invoke Claude or Codex at runtime.

## Install or migrate

1. Read [references/failover.md](references/failover.md).
2. Confirm only one host will have the timer enabled.
3. Create an environment file outside the repository from `env.example`.
4. Stop the old timer and copy its `state.sqlite3` when available.
5. Install from this skill directory:

   ```bash
   sudo scripts/install.sh \
     --env-file /path/to/vllm-fast-ci-failure-alert.env
   ```

Use `--no-start` when preparing a replacement before cutover. Because the
timer uses `Persistent=true`, `enable --now` may immediately run a missed
schedule.

## Check status

```bash
systemctl list-timers vllm-fast-ci-failure-alert.timer
systemctl status vllm-fast-ci-failure-alert.timer
journalctl -u vllm-fast-ci-failure-alert.service -n 100 --no-pager
```

The service is `Type=oneshot`; `inactive (dead)` between successful runs is
normal. Confirm the timer is `active (waiting)`.

## Run safely

Load the installed environment without printing it:

```bash
set -a
source /etc/vllm-fast-ci-failure-alert.env
set +a
```

Preview live Databricks matches without posting or changing deduplication
state:

```bash
.venv/bin/vllm-fast-ci-failure-alert \
  --state-path /var/lib/vllm-fast-ci-failure-alert/state.sqlite3 \
  --dry-run
```

Run a live alert check only when the user explicitly requests the external
Slack write. Prefer the service so the execution is journaled:

```bash
sudo systemctl start vllm-fast-ci-failure-alert.service
```

`--test-slack` sends a clearly labeled message to the configured channel. Use
it only with explicit approval to send that message.

## Change the automation

Edit `scripts/fast_ci_failure_alert.py`, keep secrets out of the repository,
and run:

```bash
uv sync --all-groups
uv run ruff check scripts
uv run ruff format --check scripts
uv run pytest
bash -n scripts/install.sh
```

Re-run `scripts/install.sh` after changing unit templates or dependencies.
The installed service contains the clone's absolute path, so reinstall after
moving the clone.
