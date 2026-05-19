# GPU Reporter Setup Guide

Reports GPU memory utilization from managed nodes to the vLLM Dashboard at https://vllm-ci-dashboard.vercel.app/gpu.

## Prerequisites

- Node must have `nvidia-smi` installed (comes with NVIDIA drivers)
- Node must have Python 3 (`/usr/bin/python3`)
- Node must have outbound HTTPS access to `vllm-ci-dashboard.vercel.app`
- SSH access to the target node

## Configuration

- **Dashboard URL**: `https://vllm-ci-dashboard.vercel.app/api/gpu/report`
- **Secret**: `gpu_report_secret` (set as `GPU_REPORT_SECRET` env var in both the Vercel dashboard and the systemd service)

## Setup Steps

1. Create the directory and copy the script:

```bash
ssh <hostname> "sudo mkdir -p /opt/gpu-reporter"
scp scripts/gpu-reporter/gpu-reporter.py <hostname>:/tmp/gpu-reporter.py
ssh <hostname> "sudo mv /tmp/gpu-reporter.py /opt/gpu-reporter/gpu-reporter.py && sudo chmod +x /opt/gpu-reporter/gpu-reporter.py"
```

2. Create the systemd service at `/etc/systemd/system/gpu-reporter.service`:

```ini
[Unit]
Description=Report GPU utilization to vLLM Dashboard

[Service]
Type=oneshot
Environment=GPU_REPORT_URL=https://vllm-ci-dashboard.vercel.app/api/gpu/report
Environment=GPU_REPORT_SECRET=gpu_report_secret
ExecStart=/usr/bin/python3 /opt/gpu-reporter/gpu-reporter.py
TimeoutSec=30
```

3. Create the systemd timer at `/etc/systemd/system/gpu-reporter.timer`:

```ini
[Unit]
Description=Run GPU reporter every 30 seconds

[Timer]
OnBootSec=10
OnUnitActiveSec=30

[Install]
WantedBy=timers.target
```

4. Enable and start:

```bash
ssh <hostname> "sudo systemctl daemon-reload && sudo systemctl enable --now gpu-reporter.timer"
```

## Verification

Test a manual run and confirm it succeeds:

```bash
ssh <hostname> "sudo systemctl start gpu-reporter.service && sudo journalctl -u gpu-reporter.service --no-pager -n 5"
```

Expected output: `OK: <N> GPUs reported for <hostname>`

Verify data appears in the API:

```bash
curl -s "https://vllm-ci-dashboard.vercel.app/api/gpu?hours=1" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['hostnames'])"
```

## Nodes Currently Running

| Hostname   | GPUs | Date Set Up |
|------------|------|-------------|
| h200-ci-2  | 8    | 2026-05-19  |

## Troubleshooting

- **"Unauthorized" from API**: Check that `GPU_REPORT_SECRET` in the systemd service matches the Vercel env var
- **"nvidia-smi failed"**: NVIDIA drivers not installed or GPU not detected
- **Timer not firing**: Run `systemctl list-timers | grep gpu` to check timer status
- **Stale data on dashboard**: GPU rows dim after 5 minutes without a report — check `systemctl status gpu-reporter.timer` on the node
