#!/usr/bin/env bash
# Move Docker + containerd data roots under a target directory.
# Example: ./move-docker-containerd.sh /dev/shm   (tmpfs, RAM-backed, ephemeral)
#          ./move-docker-containerd.sh /mnt/fast-nvme
set -euo pipefail

usage() {
  cat >&2 <<EOF
Usage: $0 <target-dir>
  Moves Docker data-root to <target-dir>/docker
  Moves containerd root to <target-dir>/containerd
EOF
  exit 2
}

[[ $# -eq 1 ]] || usage
TARGET=$1
[[ $TARGET = /* ]] || { echo "target must be an absolute path" >&2; exit 2; }
[[ -d $TARGET ]] || { echo "target dir does not exist: $TARGET" >&2; exit 2; }

DOCKER_ROOT="$TARGET/docker"
CONTAINERD_ROOT="$TARGET/containerd"
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
log "  docker     -> $DOCKER_ROOT"
log "  containerd -> $CONTAINERD_ROOT"

log "Creating target dirs on /dev/shm"
mkdir -p "$DOCKER_ROOT" "$CONTAINERD_ROOT"

# --- Docker: merge data-root into existing daemon.json ----------------------
log "Updating /etc/docker/daemon.json (backup: daemon.json.bak.$TS)"
cp /etc/docker/daemon.json "/etc/docker/daemon.json.bak.$TS"
tmp=$(mktemp)
jq --arg root "$DOCKER_ROOT" '. + {"data-root": $root}' /etc/docker/daemon.json > "$tmp"
mv "$tmp" /etc/docker/daemon.json
chmod 644 /etc/docker/daemon.json
log "New daemon.json:"
cat /etc/docker/daemon.json

# --- containerd: set root + state ------------------------------------------
log "Updating /etc/containerd/config.toml (backup: config.toml.bak.$TS)"
cp /etc/containerd/config.toml "/etc/containerd/config.toml.bak.$TS"
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

# --- systemd drop-ins to recreate dirs on every boot -----------------------
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

# --- restart: docker depends on containerd ---------------------------------
log "Stopping docker, then containerd"
systemctl stop docker.socket docker.service
systemctl stop containerd.service

log "Starting containerd, then docker"
systemctl start containerd.service
systemctl start docker.service

# --- verify ----------------------------------------------------------------
log "Verification:"
echo "--- docker info ---"
docker info 2>/dev/null | grep -E "Docker Root Dir|Storage Driver|Server Version"
echo "--- containerd ---"
ls -ld "$CONTAINERD_ROOT"
echo "--- mount check ---"
df -h /dev/shm
echo "--- smoke test ---"
docker run --rm hello-world >/dev/null 2>&1 && echo "docker run OK" || echo "docker run FAILED (image pull may need network)"

log "Done."
