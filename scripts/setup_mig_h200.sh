#!/usr/bin/env bash
# setup_mig_h200.sh — Enable MIG mode and create 18GB slices on all H200 GPUs
#
# Profile: 1g.18gb — 7 instances per GPU on H200 (143GB)
#
# Usage:
#   sudo ./setup_mig_h200.sh

set -euo pipefail

# ── Profile selection ─────────────────────────────────────────────────────────
GI_PROFILE="1g.18gb"
GI_PROFILE_ID=19

# ── Helpers ───────────────────────────────────────────────────────────────────
info()  { echo "[INFO]  $*"; }
warn()  { echo "[WARN]  $*" >&2; }
die()   { echo "[ERROR] $*" >&2; exit 1; }

require_root() {
    [[ $EUID -eq 0 ]] || die "This script must be run as root (sudo)."
}

get_gpu_ids() {
    nvidia-smi --query-gpu=index --format=csv,noheader
}

# ── Enable MIG mode ───────────────────────────────────────────────────────────
enable_mig_mode() {
    local needs_reboot=false

    info "Enabling MIG mode on all GPUs..."
    while IFS= read -r gpu; do
        local current
        current=$(nvidia-smi --query-gpu=mig.mode.current --format=csv,noheader -i "$gpu" | tr -d '[:space:]')
        if [[ "$current" == "Enabled" ]]; then
            info "  GPU $gpu: MIG already enabled, skipping"
        else
            info "  GPU $gpu: enabling MIG mode"
            if ! nvidia-smi -i "$gpu" -mig 1; then
                warn "  GPU $gpu: enabling MIG returned non-zero — may need reboot"
                needs_reboot=true
            fi
        fi
    done < <(get_gpu_ids)

    if $needs_reboot; then
        die "One or more GPUs require a reboot to activate MIG mode. Reboot and re-run this script."
    fi
}

# ── Create GPU instances ──────────────────────────────────────────────────────
create_gpu_instances() {
    info "Creating GPU instances (profile: $GI_PROFILE) on all GPUs..."
    while IFS= read -r gpu; do
        # Check how many instances this profile allows
        # Line format: |  <gpu>  MIG <name>  <id>  <free>/<total>  ...
        # Fields:        $1  $2   $3   $4     $5      $6
        local free_slots
        free_slots=$(nvidia-smi mig -lgip -i "$gpu" 2>/dev/null \
            | awk -v pid="$GI_PROFILE_ID" '$5 == pid { split($6, a, "/"); print a[1] }')

        if [[ -z "$free_slots" || "$free_slots" -eq 0 ]]; then
            warn "  GPU $gpu: no free slots for profile $GI_PROFILE (already configured?), skipping"
            continue
        fi

        info "  GPU $gpu: creating $free_slots × $GI_PROFILE instance(s)"
        for (( i=0; i<free_slots; i++ )); do
            nvidia-smi mig -cgi "$GI_PROFILE_ID" -i "$gpu" \
                || warn "  GPU $gpu: failed to create GI instance #$i"
        done
    done < <(get_gpu_ids)
}

# ── Create compute instances ──────────────────────────────────────────────────
create_compute_instances() {
    info "Creating compute instances inside all GPU instances..."
    while IFS= read -r gpu; do
        # Get all GPU instance IDs on this GPU
        # Line format: |  <gpu>  MIG <name>  <profile_id>  <instance_id>  <placement>
        # Fields:        $1  $2   $3   $4       $5              $6              $7
        local gi_ids
        mapfile -t gi_ids < <(
            nvidia-smi mig -lgi -i "$gpu" 2>/dev/null \
            | awk '/MIG/ && $6 ~ /^[0-9]+$/ { print $6 }' || true
        )

        if [[ ${#gi_ids[@]} -eq 0 ]]; then
            warn "  GPU $gpu: no GPU instances found, skipping CI creation"
            continue
        fi

        for gi_id in "${gi_ids[@]}"; do
            # Look up the default (first) CI profile ID for this GI
            local ci_profile_id
            ci_profile_id=$(nvidia-smi mig -lcip -gi "$gi_id" -i "$gpu" 2>/dev/null \
                | awk '/MIG/ { print $5; exit }' || true)

            if [[ -z "$ci_profile_id" ]]; then
                warn "  GPU $gpu / GI $gi_id: could not determine CI profile ID, skipping"
                continue
            fi

            info "  GPU $gpu / GI $gi_id: creating compute instance (profile ID $ci_profile_id)"
            nvidia-smi mig -cci "$ci_profile_id" -gi "$gi_id" -i "$gpu" \
                || warn "  GPU $gpu / GI $gi_id: CI creation failed (may already exist)"
        done
    done < <(get_gpu_ids)
}

# ── Status report ─────────────────────────────────────────────────────────────
print_status() {
    info "MIG instance summary:"
    nvidia-smi -L
    echo ""
    nvidia-smi mig -lgi 2>/dev/null || true
    echo ""
    nvidia-smi mig -lci 2>/dev/null || true
}

# ── Main ──────────────────────────────────────────────────────────────────────
require_root

enable_mig_mode
create_gpu_instances
create_compute_instances
print_status

info "Done. Profile: $GI_PROFILE across $(get_gpu_ids | wc -l) GPUs."
