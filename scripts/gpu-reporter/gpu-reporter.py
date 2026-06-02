#!/usr/bin/env python3
"""
Lightweight GPU utilization reporter for vLLM Dashboard.

Queries nvidia-smi and POSTs GPU metrics to the dashboard API.
Run via cron or systemd timer every 30-60 seconds.

Usage:
  GPU_REPORT_URL=https://your-dashboard.vercel.app/api/gpu/report \
  GPU_REPORT_SECRET=your-secret \
  python3 gpu-reporter.py

Environment variables:
  GPU_REPORT_URL    - Dashboard API endpoint (required)
  GPU_REPORT_SECRET - Bearer token for auth (optional, must match dashboard's GPU_REPORT_SECRET)
  GPU_HOSTNAME      - Override hostname (default: system hostname)
"""

import json
import os
import socket
import subprocess
import sys
import urllib.request

REPORT_URL = os.environ.get("GPU_REPORT_URL", "")
REPORT_SECRET = os.environ.get("GPU_REPORT_SECRET", "")
HOSTNAME = os.environ.get("GPU_HOSTNAME", socket.gethostname())

NVIDIA_SMI_QUERY = (
    "index,name,utilization.gpu,memory.used,memory.total,"
    "temperature.gpu,power.draw,power.limit"
)


def query_gpus():
    result = subprocess.run(
        [
            "nvidia-smi",
            f"--query-gpu={NVIDIA_SMI_QUERY}",
            "--format=csv,noheader,nounits",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        print(f"nvidia-smi failed: {result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)

    gpus = []
    for line in result.stdout.strip().split("\n"):
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 8:
            continue

        def safe_float(v):
            try:
                return float(v)
            except (ValueError, TypeError):
                return None

        gpus.append({
            "index": int(parts[0]),
            "name": parts[1] if parts[1] != "[N/A]" else None,
            "gpu_util": safe_float(parts[2]) or 0,
            "mem_used_mb": safe_float(parts[3]) or 0,
            "mem_total_mb": safe_float(parts[4]) or 0,
            "temperature_c": safe_float(parts[5]),
            "power_draw_w": safe_float(parts[6]),
            "power_limit_w": safe_float(parts[7]),
        })
    return gpus


def report(gpus):
    payload = json.dumps({"hostname": HOSTNAME, "gpus": gpus}).encode()

    headers = {"Content-Type": "application/json"}
    if REPORT_SECRET:
        headers["Authorization"] = f"Bearer {REPORT_SECRET}"

    req = urllib.request.Request(REPORT_URL, data=payload, headers=headers, method="POST")

    with urllib.request.urlopen(req, timeout=15) as resp:
        body = json.loads(resp.read())
        print(f"OK: {body.get('gpus', 0)} GPUs reported for {HOSTNAME}")


def main():
    if not REPORT_URL:
        print("GPU_REPORT_URL not set", file=sys.stderr)
        sys.exit(1)

    gpus = query_gpus()
    if not gpus:
        print("No GPUs found", file=sys.stderr)
        sys.exit(1)

    report(gpus)


if __name__ == "__main__":
    main()
