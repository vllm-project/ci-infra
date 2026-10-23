# ==========================================
# Docker & BuildKit Cache + Git Hooks Cleanup
# ==========================================
# Injected verbatim into builder agent startup scripts.
#
# Prevents root disk exhaustion on builder VMs (250 GB on cpu_64_core, 100 GB on
# cpu/release) from three sources:
# 1. Unbounded BuildKit cache growth when /etc/docker/daemon.json has no GC
#    policy configured. Uses percentage-based reservedSpace/maxUsedSpace/minFreeSpace
#    merged via python3 so existing daemon.json keys are preserved and thresholds
#    scale automatically across 100 GB (96G) and 250 GB (242G) disks.
# 2. Leftover tagged images and leaked in-memory BuildKit active references
#    (InUse=true / buildkit/lease.temporary) in long-running dockerd processes
#    that cause `docker builder prune -a -f` to reclaim 0B until docker.service
#    is restarted on an idle host.
# 3. Stale Git LFS hooks (.git/hooks/{post-checkout,post-commit,post-merge,pre-push})
#    written into shared host checkout directories by containerized steps, which
#    break subsequent host `git checkout` runs if git-lfs is missing or stale.

sudo mkdir -p /etc/docker
DAEMON_JSON_CHANGED=$(sudo python3 - <<'PY'
import json
import pathlib
import shutil

path = pathlib.Path("/etc/docker/daemon.json")
try:
    current = json.loads(path.read_text()) if path.exists() and path.stat().st_size > 0 else {}
    if not isinstance(current, dict):
        current = {}
except Exception:
    current = {}

# Moby 29.8.2 parses BuilderGCRule fields (reservedSpace, maxUsedSpace,
# minFreeSpace) with units.RAMInBytes, which requires byte suffixes (GB) rather
# than percentage strings ('%'). Compute disk-proportional GB values from the
# root filesystem size (~242 GiB on 250 GB VMs, ~96 GiB on 100 GB VMs).
total_gb = max(50, round(shutil.disk_usage("/").total / (1024 ** 3)))
pct_gb = lambda p: f"{max(5, round(total_gb * p / 100))}GB"

desired_builder = {
    "gc": {
        "enabled": True,
        "policy": [
            {"reservedSpace": pct_gb(10), "maxUsedSpace": pct_gb(20), "minFreeSpace": pct_gb(25), "filter": ["until=24h"]},
            {"reservedSpace": pct_gb(15), "maxUsedSpace": pct_gb(25), "minFreeSpace": pct_gb(20), "filter": ["until=6h"]},
            {"reservedSpace": pct_gb(20), "maxUsedSpace": pct_gb(30), "minFreeSpace": pct_gb(15), "all": True},
        ],
    }
}

if current.get("builder") != desired_builder:
    current["builder"] = desired_builder
    path.write_text(json.dumps(current, indent=2) + "\n")
    print("1")
else:
    print("0")
PY
)

# Coordinate Buildkite job startup with idle Docker cleanup/restarts via flock
# on /run/ci-docker-cache-cleanup.lock so a newly assigned job waits in
# pre-bootstrap if ci-docker-cache-cleanup.sh is currently pruning or restarting
# docker.service. Preserve any existing pre-bootstrap hook (e.g. ci_release).
sudo install -m 0666 /dev/null /run/ci-docker-cache-cleanup.lock
cat <<'TMPFILES' | sudo tee /etc/tmpfiles.d/ci-docker-cache-cleanup.conf > /dev/null
f /run/ci-docker-cache-cleanup.lock 0666 root root -
TMPFILES

sudo mkdir -p /etc/buildkite-agent/hooks
if [ -f /etc/buildkite-agent/hooks/pre-bootstrap ]; then
  if ! grep -q "ci-docker-cache-cleanup.lock" /etc/buildkite-agent/hooks/pre-bootstrap; then
    sudo sed -i '2i flock -w 180 /run/ci-docker-cache-cleanup.lock true || true' /etc/buildkite-agent/hooks/pre-bootstrap
  fi
else
  cat <<'PRE_BOOTSTRAP' | sudo tee /etc/buildkite-agent/hooks/pre-bootstrap > /dev/null
#!/bin/bash
flock -w 180 /run/ci-docker-cache-cleanup.lock true || true
PRE_BOOTSTRAP
fi
sudo chown root:root /etc/buildkite-agent/hooks/pre-bootstrap
sudo chmod 755 /etc/buildkite-agent/hooks/pre-bootstrap

cat <<'CLEAN_SCRIPT' | sudo tee /usr/local/bin/ci-docker-cache-cleanup.sh > /dev/null
#!/usr/bin/env bash
set -euo pipefail

LOCK_FILE="/run/ci-docker-cache-cleanup.lock"
exec 9>"$LOCK_FILE"
chmod 0666 "$LOCK_FILE" 2>/dev/null || true
# Exit immediately if another cleanup instance or pre-bootstrap hook holds the lock.
flock -n 9 || exit 0

is_agent_busy() {
  # 1. Any Buildkite job runner or subcommand process.
  if pgrep -f "buildkite-agent (bootstrap|meta-data|artifact|secret|step|pipeline)" >/dev/null 2>&1; then
    return 0
  fi
  # 2. Any child process of the buildkite-agent daemon (hooks, bootstrap, scripts).
  local agent_pids
  agent_pids=$(pgrep -d, -x buildkite-agent 2>/dev/null || true)
  if [ -n "$agent_pids" ] && pgrep -P "$agent_pids" >/dev/null 2>&1; then
    return 0
  fi
  # 3. Any active docker CLI operation.
  if pgrep -f "(^|/)(docker|docker-buildx) (build|buildx|push|pull|run|exec|cp|tag|rmi|rm|create|compose|login|manifest|inspect|system|builder|image|container|volume|load|save)" >/dev/null 2>&1; then
    return 0
  fi
  # 4. Any running container (including detached docker run -d / compose containers).
  if [ -n "$(docker ps -q 2>/dev/null)" ]; then
    return 0
  fi
  return 1
}

get_use_pct() {
  df --output=pcent / | tail -n1 | tr -dc "0-9"
}

# Never mutate .git/hooks, /tmp, images, or build cache while a job is active.
if is_agent_busy; then
  exit 0
fi

# Brief settle check: if a job was accepted right as we acquired the lock, it
# will either show up in is_agent_busy now or block in pre-bootstrap on LOCK_FILE.
sleep 1
if is_agent_busy; then
  exit 0
fi

# If daemon.json was updated while a job was running, apply the pending restart
# now that the host is confirmed idle.
if [ -f /run/ci-docker-daemon-reload-needed ] && ! is_agent_busy; then
  logger -t ci-docker-cache-cleanup "restarting docker.service while idle to apply updated /etc/docker/daemon.json"
  systemctl restart docker
  rm -f /run/ci-docker-daemon-reload-needed
fi

# Host is confirmed idle and locked against new jobs starting checkout/build:
# 1. Remove stale Git LFS hooks written into host build checkouts (depth-bounded
#    so find never walks full repository working trees).
find /var/lib/buildkite-agent/builds -maxdepth 6 -path "*/.git/hooks/*" \
  \( -name post-checkout -o -name post-commit -o -name post-merge -o -name pre-push \) \
  -delete 2>/dev/null || true

# 2. Remove leftover temporary Buildkite hook wrappers, env files, and orphaned
#    deploy keys older than 60 minutes on this idle host.
find /tmp -maxdepth 1 -name "buildkite-agent-*" -mmin +60 -exec rm -rf {} + 2>/dev/null || true

# 3. Prune stopped containers and unused volumes.
docker container prune -f >/dev/null 2>&1 || true
docker volume prune -f >/dev/null 2>&1 || true

# 4. Tiered image pruning:
#    `docker image prune --filter "until=..."` checks the image manifest's
#    Created timestamp, NOT when the image was pulled or last used. When disk
#    usage is healthy (<50%), prune dangling images only (NO `-a`) so pulled
#    upstream base images stay warm across builds.
USE_PCT=$(get_use_pct)
if [ -n "$USE_PCT" ] && [ "$USE_PCT" -ge 65 ]; then
  logger -t ci-docker-cache-cleanup "root disk at ${USE_PCT}% (>=65%), pruning all unused Docker images"
  docker image prune -a -f >/dev/null 2>&1 || true
elif [ -n "$USE_PCT" ] && [ "$USE_PCT" -ge 50 ]; then
  logger -t ci-docker-cache-cleanup "root disk at ${USE_PCT}% (50-64%), pruning unused Docker images with Created >24h"
  docker image prune -a -f --filter "until=24h" >/dev/null 2>&1 || true
else
  docker image prune -f --filter "until=6h" >/dev/null 2>&1 || true
fi

# 5. Disk-size-aware BuildKit cache prune (~20% normal, ~12% low):
#    - On 250 GB (242G) disk: KEEP_NORMAL_GB=48, KEEP_LOW_GB=29
#    - On 100 GB (96G) disk:  KEEP_NORMAL_GB=19, KEEP_LOW_GB=11 (keeps post-prune usage <30%, well below 45%)
ROOT_SIZE_GB=$(df -BG --output=size / | tail -n1 | tr -dc "0-9")
KEEP_NORMAL_GB=$(( ${ROOT_SIZE_GB:-100} * 20 / 100 ))
KEEP_LOW_GB=$(( ${ROOT_SIZE_GB:-100} * 12 / 100 ))
[ "$KEEP_NORMAL_GB" -lt 10 ] && KEEP_NORMAL_GB=10
[ "$KEEP_LOW_GB" -lt 6 ] && KEEP_LOW_GB=6

# First prune BuildKit cache to KEEP_NORMAL_GB without restarting docker.service.
docker builder prune -a --keep-storage "${KEEP_NORMAL_GB}GB" -f >/dev/null 2>&1 || true

# 6. Only restart docker.service if disk usage is still >= 45% while idle AND
#    BuildKit holds leaked in-memory ACTIVE cache references (Active > 0).
USE_PCT=$(get_use_pct)
if [ -n "$USE_PCT" ] && [ "$USE_PCT" -ge 45 ] && ! is_agent_busy; then
  ACTIVE_CACHE=$(docker system df --format '{{if eq .Type "Build Cache"}}{{.Active}}{{end}}' 2>/dev/null | tr -dc "0-9")
  if [ "${ACTIVE_CACHE:-0}" -gt 0 ]; then
    logger -t ci-docker-cache-cleanup "root disk at ${USE_PCT}% with ${ACTIVE_CACHE} active BuildKit cache records while idle; restarting docker.service to release leaked references"
    systemctl restart docker
    docker builder prune -a --keep-storage "${KEEP_LOW_GB}GB" -f >/dev/null 2>&1 || true
  fi
  USE_PCT=$(get_use_pct)
  if [ -n "$USE_PCT" ] && [ "$USE_PCT" -ge 55 ] && ! is_agent_busy; then
    logger -t ci-docker-cache-cleanup "root disk still at ${USE_PCT}% (>=55%) after keep-storage prune; pruning all BuildKit cache"
    docker builder prune -a -f >/dev/null 2>&1 || true
  fi
fi
CLEAN_SCRIPT
sudo chmod +x /usr/local/bin/ci-docker-cache-cleanup.sh

# Only restart docker.service during provisioning if /etc/docker/daemon.json
# actually changed (and only when no job/container is active).
if [ "${DAEMON_JSON_CHANGED:-0}" = "1" ] || ! systemctl is-active --quiet docker.service; then
  AGENT_PIDS=$(pgrep -d, -x buildkite-agent 2>/dev/null || true)
  if pgrep -f "buildkite-agent (bootstrap|meta-data|artifact|secret|step|pipeline)" >/dev/null 2>&1 || \
     { [ -n "$AGENT_PIDS" ] && pgrep -P "$AGENT_PIDS" >/dev/null 2>&1; } || \
     pgrep -f "(^|/)(docker|docker-buildx) (build|buildx|push|pull|run|exec|cp|tag|rmi|rm|create|compose|login|manifest|inspect|system|builder|image|container|volume|load|save)" >/dev/null 2>&1 || \
     [ -n "$(docker ps -q 2>/dev/null)" ]; then
    sudo touch /run/ci-docker-daemon-reload-needed
  else
    sudo systemctl reset-failed docker.service 2>/dev/null || true
    sudo systemctl restart docker
    sudo rm -f /run/ci-docker-daemon-reload-needed
  fi
fi

cat <<'CLEAN_UNIT' | sudo tee /etc/systemd/system/ci-docker-cache-cleanup.service > /dev/null
[Unit]
Description=Buildkite CI Docker Image, BuildKit Cache, and Git Hooks Cleanup
After=docker.service buildkite-agent.service

[Service]
Type=oneshot
ExecStart=/usr/local/bin/ci-docker-cache-cleanup.sh
CLEAN_UNIT

cat <<'CLEAN_TIMER' | sudo tee /etc/systemd/system/ci-docker-cache-cleanup.timer > /dev/null
[Unit]
Description=Run CI Docker & BuildKit Cache Cleanup every 30 minutes

[Timer]
OnBootSec=5min
OnUnitActiveSec=30min
RandomizedDelaySec=60

[Install]
WantedBy=timers.target
CLEAN_TIMER

sudo systemctl daemon-reload
sudo systemctl enable --now ci-docker-cache-cleanup.timer
sudo systemctl restart ci-docker-cache-cleanup.timer
# ==========================================
