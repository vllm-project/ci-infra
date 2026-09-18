#!/usr/bin/env bash
# Move Docker + containerd data roots (and the Buildkite build dir, if present)
# under a target directory on a large volume.
#
# Why: vLLM CI images are ~34GB each and a busy GPU machine accumulates several
# (plus pull-through-cache duplicates). On a small root disk they fill it up,
# after which Buildkite agents stay connected but every job fails at init with
# "no space left on device" (writing /tmp/job-env-*). Moving storage off the
# root disk prevents that.
#
# Example: ./move-docker-containerd.sh /dev/shm   (tmpfs, RAM-backed, ephemeral)
#          ./move-docker-containerd.sh /mnt/local
#
# Notes:
# - Existing images/volumes are NOT migrated; the new roots start empty and
#   images are re-pulled on demand. (docker pull caches rebuild quickly; if you
#   need the old data, copy /var/lib/docker and /var/lib/containerd yourself
#   before running.)
# - Works on a fresh machine: creates /etc/docker/daemon.json and
#   /etc/containerd/config.toml if they don't exist. Uses jq if available,
#   otherwise python3 (present on stock Ubuntu).
set -euo pipefail

usage() {
  cat >&2 <<EOF
Usage: $0 <target-dir>
  Moves Docker data-root to <target-dir>/docker
  Moves containerd root to <target-dir>/containerd
  If buildkite-agent is installed, also moves build-path to
  <target-dir>/buildkite-agent/builds
EOF
  exit 2
}

[[ $# -eq 1 ]] || usage
TARGET=$1
[[ $TARGET = /* ]] || { echo "target must be an absolute path" >&2; exit 2; }
[[ -d $TARGET ]] || { echo "target dir does not exist: $TARGET" >&2; exit 2; }

DOCKER_ROOT="$TARGET/docker"
CONTAINERD_ROOT="$TARGET/containerd"
BK_BUILDS="$TARGET/buildkite-agent/builds"
TS=$(date +%Y%m%d-%H%M%S)

log() { echo "[$(date +%H:%M:%S)] $*"; }

require_root() {
  if [[ $EUID -ne 0 ]]; then
    log "Re-executing under sudo..."
    exec sudo -E bash "$0" "$@"
  fi
}
require_root "$@"

log "Target: $TARGET"
log "  docker       -> $DOCKER_ROOT"
log "  containerd   -> $CONTAINERD_ROOT"

log "Creating target dirs"
mkdir -p "$DOCKER_ROOT" "$CONTAINERD_ROOT"

# --- Docker: merge data-root into daemon.json (create if missing) ------------
log "Updating /etc/docker/daemon.json (backup: daemon.json.bak.$TS)"
if [[ -f /etc/docker/daemon.json ]]; then
  cp /etc/docker/daemon.json "/etc/docker/daemon.json.bak.$TS"
else
  log "  /etc/docker/daemon.json does not exist, creating a fresh one"
  echo '{}' > /etc/docker/daemon.json
fi
tmp=$(mktemp)
if command -v jq >/dev/null 2>&1; then
  jq --arg root "$DOCKER_ROOT" '. + {"data-root": $root}' /etc/docker/daemon.json > "$tmp"
else
  log "  jq not found, using python3"
  DOCKER_ROOT="$DOCKER_ROOT" python3 - <<'EOF' > "$tmp"
import json, os
with open("/etc/docker/daemon.json") as f:
    cfg = json.load(f)
cfg["data-root"] = os.environ["DOCKER_ROOT"]
print(json.dumps(cfg, indent=2))
EOF
fi
mv "$tmp" /etc/docker/daemon.json
chmod 644 /etc/docker/daemon.json
log "New daemon.json:"
cat /etc/docker/daemon.json

# --- containerd: set root + state (create config if missing) -----------------
log "Updating /etc/containerd/config.toml (backup: config.toml.bak.$TS)"
if [[ -f /etc/containerd/config.toml ]]; then
  cp /etc/containerd/config.toml "/etc/containerd/config.toml.bak.$TS"
else
  log "  /etc/containerd/config.toml does not exist, creating a fresh one"
  mkdir -p /etc/containerd
  : > /etc/containerd/config.toml
fi
# Strip any existing root=/state= lines we may have added before, then prepend.
sed -i -E '/^[[:space:]]*root[[:space:]]*=.*$/d; /^[[:space:]]*state[[:space:]]*=.*$/d' /etc/containerd/config.toml
{
  echo "root = \"$CONTAINERD_ROOT\""
  echo "state = \"/run/containerd\""
  cat /etc/containerd/config.toml
} > "$tmp"
mv "$tmp" /etc/containerd/config.toml
chmod 644 /etc/containerd/config.toml
log "New containerd config (head):"
head -5 /etc/containerd/config.toml

# --- buildkite-agent: move build-path onto the same volume -------------------
if [[ -f /etc/buildkite-agent/buildkite-agent.cfg ]]; then
  log "buildkite-agent config found, moving build-path -> $BK_BUILDS"
  mkdir -p "$BK_BUILDS"
  if grep -q '^build-path=' /etc/buildkite-agent/buildkite-agent.cfg; then
    cp /etc/buildkite-agent/buildkite-agent.cfg "/etc/buildkite-agent/buildkite-agent.cfg.bak.$TS"
    sed -i -E "s|^build-path=.*|build-path=\"$BK_BUILDS\"|" /etc/buildkite-agent/buildkite-agent.cfg
  else
    echo "build-path=\"$BK_BUILDS\"" >> /etc/buildkite-agent/buildkite-agent.cfg
  fi
  # Migrate existing build checkouts if the old dir exists and has content.
  old_builds=$(sed -n -E "s|^build-path=\"?(.*?)\"?\$|\1|p" "/etc/buildkite-agent/buildkite-agent.cfg.bak.$TS" 2>/dev/null || true)
  if [[ -n "${old_builds:-}" && -d "$old_builds" && "$old_builds" != "$BK_BUILDS" ]]; then
    log "  migrating existing builds from $old_builds"
    cp -a "$old_builds/." "$BK_BUILDS/" 2>/dev/null || true
    rm -rf "$old_builds"
  fi
  id buildkite-agent >/dev/null 2>&1 && chown -R buildkite-agent:buildkite-agent "$TARGET/buildkite-agent"
else
  log "buildkite-agent not installed yet, skipping build-path (set it to $BK_BUILDS when you install it)"
fi

# --- systemd drop-ins to recreate dirs on every boot -------------------------
log "Writing systemd drop-ins"
mkdir -p /etc/systemd/system/docker.service.d /etc/systemd/system/containerd.service.d
# Remove any drop-in from an earlier run (legacy name + current name) so a
# stale ExecStartPre for a different target doesn't linger.
rm -f /etc/systemd/system/docker.service.d/shm-data-root.conf \
      /etc/systemd/system/containerd.service.d/shm-data-root.conf \
      /etc/systemd/system/docker.service.d/custom-data-root.conf \
      /etc/systemd/system/containerd.service.d/custom-data-root.conf

cat > /etc/systemd/system/docker.service.d/custom-data-root.conf <<EOF
[Service]
ExecStartPre=/bin/mkdir -p $DOCKER_ROOT
EOF

cat > /etc/systemd/system/containerd.service.d/custom-data-root.conf <<EOF
[Service]
ExecStartPre=/bin/mkdir -p $CONTAINERD_ROOT
EOF

systemctl daemon-reload

# --- restart: docker depends on containerd -----------------------------------
log "Stopping docker, then containerd"
systemctl stop docker.socket docker.service 2>/dev/null || true
systemctl stop containerd.service 2>/dev/null || true

log "Starting containerd, then docker"
systemctl start containerd.service
systemctl start docker.service

# --- verify ------------------------------------------------------------------
log "Verification:"
echo "--- docker info ---"
docker info 2>/dev/null | grep -E "Docker Root Dir|Storage Driver|Server Version"
echo "--- containerd ---"
ls -ld "$CONTAINERD_ROOT"
echo "--- target mount ---"
df -h "$TARGET" | tail -1
echo "--- root mount (should have space now) ---"
df -h / | tail -1
echo "--- smoke test ---"
docker run --rm hello-world >/dev/null 2>&1 && echo "docker run OK" || echo "docker run FAILED (image pull may need network)"

log "Done."
