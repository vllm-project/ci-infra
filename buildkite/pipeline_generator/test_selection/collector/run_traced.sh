#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

# Capture only CUDA launches and pytest NVTX ranges, reduce them immediately to
# an unordered kernel/test set, then delete the temporary Nsight timeline.
set -euo pipefail

NSYS_DEB_URL="https://developer.download.nvidia.com/devtools/repos/ubuntu2204/amd64/NsightSystems-linux-cli-public-2026.3.1.157-3804839.deb"
NSYS_DEB_SHA256="3eb87ec08e5f8b8f153537847747bd5cfabb51b9c8793873b26a3c55dc813ad1"

if (( $# < 3 )); then
    echo "usage: $0 <output-dir> <represented-job-key> <command...>" >&2
    exit 2
fi

OUT_DIR="$1"
REPRESENTED_JOB_KEY="$2"
shift 2
mkdir -p "$OUT_DIR"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ ! "${BUILDKITE_COMMIT:-}" =~ ^[0-9a-f]{40}$ ]]; then
    echo "BUILDKITE_COMMIT must be an exact Git SHA" >&2
    exit 2
fi

install_seconds=0
if ! command -v nsys >/dev/null; then
    install_start=$(date +%s)
    deb="$(mktemp /tmp/nsys-cli-XXXX.deb)"
    curl -fsSL -o "$deb" "$NSYS_DEB_URL"
    echo "$NSYS_DEB_SHA256  $deb" | sha256sum -c -
    apt-get update -qq
    apt-get install -y -qq libglib2.0-0
    dpkg -i "$deb"
    rm -f "$deb"
    install_seconds=$(( $(date +%s) - install_start ))
fi

traced_start=$(date +%s)
set +e
nsys profile \
    --trace=cuda,nvtx --sample=none --cpuctxsw=none \
    --trace-fork-before-exec=true \
    --output "$OUT_DIR/trace" --force-overwrite=true \
    -- "$@"
profile_status=$?
set -e
traced_seconds=$(( $(date +%s) - traced_start ))

parse_status=0
created_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
if [[ -s "$OUT_DIR/trace.nsys-rep" ]]; then
    set +e
    nsys export --type sqlite \
        --output "$OUT_DIR/trace.sqlite" \
        --force-overwrite=true "$OUT_DIR/trace.nsys-rep"
    export_status=$?
    if (( export_status == 0 )); then
        python3 "$HERE/parse_nsys_sqlite.py" "$OUT_DIR/trace.sqlite" \
            --job-key "$REPRESENTED_JOB_KEY" \
            --repository-sha "$BUILDKITE_COMMIT" \
            --created-at "$created_at" \
            --out "$OUT_DIR/gpu-trace.jsonl" \
            2> "$OUT_DIR/kernel-capture-summary.json"
        parse_status=$?
    else
        parse_status=$export_status
        printf '{"error":"nsys export failed","exit_code":%d}\n' \
            "$export_status" > "$OUT_DIR/kernel-capture-summary.json"
    fi
    set -e
else
    parse_status=1
    printf '{"error":"nsys profile produced no report","exit_code":%d}\n' \
        "$profile_status" > "$OUT_DIR/kernel-capture-summary.json"
fi

# The selector consumes only the compact identity set. Do not upload or retain
# the temporary profiler timeline.
rm -f "$OUT_DIR/trace.nsys-rep" "$OUT_DIR/trace.sqlite"

printf '{"collector_install_seconds":%d,"parse_exit_code":%d,"profile_exit_code":%d,"traced_wall_seconds":%d}\n' \
    "$install_seconds" "$parse_status" "$profile_status" "$traced_seconds" \
    > "$OUT_DIR/kernel-capture-timings.json"
cat "$OUT_DIR/kernel-capture-summary.json" \
    "$OUT_DIR/kernel-capture-timings.json"

if (( profile_status != 0 )); then
    exit "$profile_status"
fi
exit "$parse_status"
