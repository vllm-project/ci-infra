---
name: vllm-nightly-perf
description: Install, migrate, operate, or diagnose the portable vLLM nightly perf-eval automation. Use when moving the nightly Buildkite trigger and Slack regression report to another Linux host, checking their systemd timers, running either job safely, or investigating missing or duplicate nightly perf reports.
---

# vLLM Nightly Perf

Operate the two deterministic jobs bundled with this skill:

- `trigger`: start `vllm/perf-eval` for the latest scheduled `release-v2`
  nightly whose `Build release image - x86_64 - CUDA 13.0` job passed. The
  aggregate release build may fail.
- `report`: compare the latest completed perf-eval run with rolling history and
  post the Block Kit report to Slack.

The jobs do not invoke Claude or Codex at runtime.

## Install or migrate

1. Read [references/failover.md](references/failover.md).
2. Confirm only one host will have the timers enabled. The trigger deduplicates
   through a best-effort Buildkite lookup, but two hosts can still race and
   create duplicate builds or post duplicate Slack messages.
3. Create an environment file outside the repository from `env.example`.
4. If the old host is reachable, stop its timers and copy its state directory.
5. From this skill directory, install on a systemd Linux host:

   ```bash
   sudo scripts/install.sh --env-file /path/to/vllm-nightly-perf.env
   ```

Use `--no-start` when preparing a replacement before cutover. Validate with
dry runs, then enable both timers explicitly. Because the timers use
`Persistent=true`, `enable --now` can immediately run a missed schedule.

## Check status

```bash
systemctl list-timers 'vllm-nightly-perf-*'
systemctl status vllm-nightly-perf-trigger.timer
systemctl status vllm-nightly-perf-report.timer
journalctl -u vllm-nightly-perf-trigger.service -n 100 --no-pager
journalctl -u vllm-nightly-perf-report.service -n 100 --no-pager
```

Treat `SKIP` as a successful no-op. Common reasons are no scheduled release
with a passed x86_64 CUDA 13.0 image job, an existing perf-eval build for the
commit, or an already-reported build.

## Run safely

Load the installed environment without printing it:

```bash
set -a
source /etc/vllm-nightly-perf.env
set +a
```

Preview without external writes:

```bash
.venv/bin/vllm-nightly-perf trigger --state-dir /var/lib/vllm-nightly-perf --dry-run
.venv/bin/vllm-nightly-perf report --state-dir /var/lib/vllm-nightly-perf --dry-run
```

Dry runs write only payload and diagnostic files in the state directory. If
the old report state is unavailable, confirm the latest Buildkite build was
already posted in Slack, then seed it explicitly:

```bash
.venv/bin/vllm-nightly-perf adopt \
  --state-dir /var/lib/vllm-nightly-perf \
  --build-number 310
```

`adopt` rejects a build that is not the dashboard's latest reportable build.

Run a live trigger or report only when the user explicitly requests the
external Buildkite or Slack write. Prefer `systemctl start
vllm-nightly-perf-*.service` so the execution is journaled.

## Change the automation

Edit `scripts/vllm_perf_eval.py`, keep secrets out of the repository, and run:

```bash
uv sync --all-groups
uv run pytest
bash -n scripts/install.sh
```

Re-run `scripts/install.sh` after changing unit templates or dependencies.
The installed unit files contain the clone's absolute path, so reinstall after
moving the clone.
