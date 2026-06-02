#!/usr/bin/env bash
# teardown_mig_h200.sh — Destroy all MIG instances and disable MIG mode on all H200 GPUs
#
# Usage:
#   sudo ./teardown_mig_h200.sh

set -euo pipefail

info() { echo "[INFO]  $*"; }
warn() { echo "[WARN]  $*" >&2; }
die()  { echo "[ERROR] $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "This script must be run as root (sudo)."

info "Destroying all compute instances..."
nvidia-smi mig -dci 2>/dev/null || true

info "Destroying all GPU instances..."
nvidia-smi mig -dgi 2>/dev/null || true

info "Disabling MIG mode on all GPUs..."
while IFS= read -r gpu; do
    info "  GPU $gpu: disabling MIG"
    nvidia-smi -i "$gpu" -mig 0 || warn "  GPU $gpu: disable failed (may need reboot)"
done < <(nvidia-smi --query-gpu=index --format=csv,noheader)

info "Done. A reboot may be required for MIG mode changes to fully take effect."
