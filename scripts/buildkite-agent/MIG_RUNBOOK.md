# H200 MIG + Buildkite Runbook

How to take a bare 8×H200 machine and turn it into MIG-sliced Buildkite agents
that report GPU stats to <https://ci.vllm.ai/gpu>. Written for someone doing
this for the first time.

## The big picture

One H200 has 143 GB of memory — far more than most CI test jobs need. **MIG
(Multi-Instance GPU)** splits each physical GPU into isolated slices, so a
single 8-GPU box can run many jobs in parallel, each on its own slice.

The pieces fit together like this:

```
8 physical H200 GPUs
   └─ MIG splits each into N slices        (nvidia-smi mig ...)
   └─ one Buildkite agent per slice        (buildkite-agent spawn=N*8)
   └─ each agent pins its job to one slice (the `environment` hook)
   └─ jobs land via a queue tag            (queue=h200_35gb)
   └─ a systemd timer reports GPU stats    (gpu-reporter → ci.vllm.ai/gpu)
```

**Pick a MIG profile first.** It decides slice size, slices per GPU, and how
many agents you run:

| Profile   | Slice mem | Slices/GPU | Agents (8 GPUs) | Buildkite queue |
|-----------|-----------|-----------:|----------------:|-----------------|
| `1g.18gb` | 18 GB     | 7          | 56              | `h200_18gb`     |
| `1g.35gb` | 35 GB     | 4          | 32              | `h200_35gb`     |

Everything downstream (slice count, `spawn`, the hook's slice math) must match
the profile you choose. This is the number-one source of setup bugs.

## Prerequisites

- Ubuntu 20.04+ (or Amazon Linux), root/sudo access
- NVIDIA driver + `nvidia-container-toolkit` installed and working
  (`nvidia-smi` shows all 8 GPUs). See `../AGENT.md` steps 1-5 for Docker, AWS
  CLI, and the toolkit.
- A Buildkite agent **registration token** for your cluster
- Secrets: `HF_TOKEN`, AWS access key + secret (for ECR pulls), and optionally
  `BUILDKITE_ANALYTICS_TOKEN`
- For monitoring: the `GPU_REPORT_SECRET` bearer token the dashboard expects

## Step 1 — Enable MIG and create slices

Use `../setup_mig_h200.sh`. It enables MIG mode on every GPU, then creates the
GPU instances and compute instances. By default it's set to the `1g.18gb`
profile — for `1g.35gb`, edit the two lines near the top:

```bash
GI_PROFILE="1g.35gb"
GI_PROFILE_ID=15    # 1g.35gb; 1g.18gb is 19
```

Then run it:

```bash
sudo ../setup_mig_h200.sh
```

If it says a reboot is needed to activate MIG mode, reboot and re-run.

Verify you have the right number of slices (7×8=56 for 18gb, 4×8=32 for 35gb):

```bash
nvidia-smi -L | grep -c MIG
```

## Step 2 — Make the slices usable by Docker (CDI)

The agent pins jobs to slices using NVIDIA **CDI** (Container Device
Interface). Generate the CDI spec so the MIG devices are visible to Docker:

```bash
sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml
nvidia-ctk cdi list | grep -c "nvidia.com/gpu=MIG-"   # should equal your slice count
```

> Re-run this any time you recreate MIG instances (e.g. after a reboot that
> didn't persist them, or after running `teardown_mig_h200.sh`).

## Step 3 — Install the Buildkite agent config and hooks

Ready-made, annotated templates live in this directory (`buildkite-agent/`).
They encode the slice→agent mapping so you don't have to write it yourself.

```bash
sudo install -m 0755 environment            /etc/buildkite-agent/hooks/environment
sudo install -m 0755 pre-checkout           /etc/buildkite-agent/hooks/pre-checkout
sudo install -m 0755 post-checkout          /etc/buildkite-agent/hooks/post-checkout
sudo install -m 0755 buildkite-gpu-cdi-env.sh /usr/local/libexec/buildkite-gpu-cdi-env.sh
sudo install -m 0644 buildkite-agent.cfg    /etc/buildkite-agent/buildkite-agent.cfg
```

Now edit **two** files to match your machine:

**`/etc/buildkite-agent/buildkite-agent.cfg`:**
- `token=` — your Buildkite agent registration token
- `name="mithril-h200-1-%spawn"` — your hostname; `%spawn` becomes the agent number
- `tags="queue=h200_35gb"` — the queue for your profile
- `spawn=32` — `num_gpus × slices_per_gpu` (32 for 35gb, 56 for 18gb)

**`/etc/buildkite-agent/hooks/environment`:**
- Fill in the secrets: `HF_TOKEN`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
  and optionally `BUILDKITE_ANALYTICS_TOKEN`
- `SLICES_PER_GPU=4` — must match the profile (4 for 35gb, 7 for 18gb)
- `HF_HOME=...` — point at large storage (see below)

> **HF_HOME:** the docker plugin passes `HF_HOME` through to containers and
> mounts a fixed path. Set `HF_HOME` to a path that is actually mounted into
> the container — for `h200_35gb` the plugin mounts `/mnt/vllm-ci` and
> `/mnt/hf-cache-af-south1-a`. Pick a big shared/local mount (4+ TB preferred).
> See `../AGENT.md` step 6 for a snippet that auto-detects the best disk.

### How the slice pinning works (the part worth understanding)

The agent spawns N copies of itself, named `<host>-1` … `<host>-N`. When each
job starts, the `environment` hook:

1. Reads the agent number from `BUILDKITE_AGENT_NAME` (the `-N` suffix)
2. Lists all MIG device UUIDs from `nvidia-smi -L`
3. Picks UUID #N and exports it as a CDI device so the job's container lands on
   exactly that slice
4. Uses an `flock` so the first image pull happens once, not 32 times at once

This is why `spawn`, `SLICES_PER_GPU`, and the actual MIG slice count must all
agree — if they don't, an agent either maps to a nonexistent slice or two
agents collide on the same one.

## Step 4 — Start the agents

```bash
sudo systemctl enable --now buildkite-agent
sudo systemctl status buildkite-agent
```

Verify all spawns connected (look for `<host>-1` … `<host>-N` ping streams):

```bash
sudo journalctl -u buildkite-agent --no-pager | grep -oP "$(hostname)-\d+" | sort -u | wc -l
```

You should see N distinct agents, and they should appear in the Buildkite
dashboard under your queue within a few seconds.

## Step 5 — Report GPU stats to ci.vllm.ai/gpu

The `../gpu-reporter/` directory has a small Python reporter that reads
`nvidia-smi` and POSTs utilization to the dashboard, plus a systemd
service + timer to run it every 30s.

```bash
# Install the script
sudo mkdir -p /opt/gpu-reporter
sudo install -m 0755 ../gpu-reporter/gpu-reporter.py /opt/gpu-reporter/gpu-reporter.py

# Install the service + timer
sudo install -m 0644 ../gpu-reporter/gpu-reporter.service /etc/systemd/system/gpu-reporter.service
sudo install -m 0644 ../gpu-reporter/gpu-reporter.timer   /etc/systemd/system/gpu-reporter.timer
```

Edit `/etc/systemd/system/gpu-reporter.service` and set the real values:

```ini
Environment=GPU_REPORT_URL=https://ci.vllm.ai/api/gpu/report
Environment=GPU_REPORT_SECRET=<the dashboard's bearer secret>
```

Then enable it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now gpu-reporter.timer

# confirm it's firing and succeeding
sudo systemctl list-timers gpu-reporter.timer
sudo journalctl -u gpu-reporter.service --no-pager -n 5   # want "OK: N GPUs reported"
```

Within a minute the host should show up at <https://ci.vllm.ai/gpu> under its
hostname.

> Note: `nvidia-smi` reports the **physical** GPUs, so the dashboard shows
> per-GPU utilization for the whole box — it doesn't break stats down per MIG
> slice. That's usually what you want for capacity monitoring.

## Verify end to end

- [ ] `nvidia-smi -L | grep -c MIG` equals your expected slice count
- [ ] `nvidia-ctk cdi list | grep -c MIG-` equals the same count
- [ ] `systemctl is-active buildkite-agent` is `active`
- [ ] N agents visible in the Buildkite dashboard on the right queue
- [ ] A test job on the queue runs and lands on a MIG slice (check the job log
      for `Agent <host>-N → GPU g slot s → MIG-...`)
- [ ] Host appears at <https://ci.vllm.ai/gpu>

## Troubleshooting

- **Agent error: "Agent number N exceeds available MIG devices"** — `spawn` is
  larger than the actual slice count. Recreate the slices (Step 1) or lower
  `spawn`.
- **Job error: "CDI device ... is absent"** — the CDI spec is stale. Re-run
  `nvidia-ctk cdi generate` (Step 2).
- **Two jobs landing on the same slice** — `SLICES_PER_GPU` doesn't match the
  profile. Fix it in the `environment` hook and restart the agent.
- **No GPU stats on the dashboard** — check
  `journalctl -u gpu-reporter.service` for a non-`OK` line; usually a wrong
  `GPU_REPORT_URL` or `GPU_REPORT_SECRET`.
- **Tear it down:** `sudo ../teardown_mig_h200.sh` destroys all slices and
  disables MIG (a reboot may be needed).

## Related files

- `../AGENT.md` — generic machine → Buildkite agent onboarding
- `../setup_mig_h200.sh` / `../teardown_mig_h200.sh` — MIG slice create/destroy
- `./` (`buildkite-agent/`) — agent config + hook templates
- `../gpu-reporter/` — GPU stats reporter for ci.vllm.ai/gpu
