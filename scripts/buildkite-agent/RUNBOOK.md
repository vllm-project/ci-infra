# GPU Machine → Buildkite Runbook

How to take a bare GPU machine and turn it into Buildkite agents that report
GPU stats to <https://ci.vllm.ai/gpu>. Written for someone doing this for the
first time.

This covers both cases:

- **Whole-GPU** — each agent gets one or more full GPUs (no slicing). Use this
  when jobs need a lot of memory, or when the machine has exactly as many GPUs
  as you want agents.
- **MIG slicing** — split each GPU into isolated slices and run one agent per
  slice, to pack many small jobs onto a big GPU. Only some GPUs (A100, H100,
  H200, B200…) support MIG, and you only need it when a whole GPU is more than
  a job requires.

## The big picture

```
Physical GPUs in the machine
   └─ (optional) MIG splits each GPU into slices   ← only if slicing
   └─ one Buildkite agent per GPU or per slice      (buildkite-agent spawn=N)
   └─ each agent pins its job to its GPU/slice      (the `environment` hook)
   └─ jobs land via a queue tag                     (e.g. queue=h200_35gb)
   └─ a systemd timer reports GPU stats             (gpu-reporter → ci.vllm.ai/gpu)
```

**Decide up front: whole-GPU or MIG?** This drives the agent count (`spawn`)
and the queue tag. If you're not slicing, `spawn` is usually just the number of
GPUs (or 1, if agents share all GPUs). If you're slicing, it's `num_gpus ×
slices_per_gpu`.

For MIG on an 8×H200, the two profiles in use are:

| Profile   | Slice mem | Slices/GPU | Agents (8 GPUs) | Buildkite queue |
|-----------|-----------|-----------:|----------------:|-----------------|
| `1g.18gb` | 18 GB     | 7          | 56              | `h200_18gb`     |
| `1g.35gb` | 35 GB     | 4          | 32              | `h200_35gb`     |

## Prerequisites

- Ubuntu 20.04+ (or Amazon Linux), root/sudo access
- NVIDIA driver + `nvidia-container-toolkit` installed and working
  (`nvidia-smi` lists your GPUs). See `../AGENT.md` steps 1-5 for Docker, AWS
  CLI, and the container toolkit.
- A Buildkite agent **registration token** for your cluster
- Secrets: `HF_TOKEN`, AWS access key + secret (for ECR pulls), and optionally
  `BUILDKITE_ANALYTICS_TOKEN`
- For monitoring: the `GPU_REPORT_SECRET` bearer token the dashboard expects

## Step 1 — (MIG only) Enable MIG and create slices

Skip this step entirely if you're running whole GPUs.

Use `../setup_mig_h200.sh`. It enables MIG mode on every GPU, then creates the
GPU instances and compute instances. It's set to `1g.18gb` by default — for
`1g.35gb`, edit the two lines near the top:

```bash
GI_PROFILE="1g.35gb"
GI_PROFILE_ID=15    # 1g.35gb; 1g.18gb is 19
```

Then run it:

```bash
sudo ../setup_mig_h200.sh
```

If it says a reboot is needed to activate MIG mode, reboot and re-run.

Verify the slice count (7×8=56 for 18gb, 4×8=32 for 35gb):

```bash
nvidia-smi -L | grep -c MIG
```

## Step 2 — Make the GPUs/slices usable by Docker (CDI)

Agents pin jobs to a GPU or MIG slice using NVIDIA **CDI** (Container Device
Interface). Generate the CDI spec so the devices are visible to Docker:

```bash
sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml
```

Verify — whole GPUs show as `nvidia.com/gpu=GPU-...`, MIG slices as
`nvidia.com/gpu=MIG-...`:

```bash
nvidia-ctk cdi list | grep -c "nvidia.com/gpu="
```

> Re-run this after recreating MIG instances, or after a reboot that didn't
> persist them (e.g. following `teardown_mig_h200.sh`).

## Step 3 — Install the Buildkite agent config and hooks

Ready-made, annotated templates live in this directory (`buildkite-agent/`).
They encode the device→agent mapping so you don't have to write it yourself.

```bash
sudo install -m 0755 environment            /etc/buildkite-agent/hooks/environment
sudo install -m 0755 pre-checkout           /etc/buildkite-agent/hooks/pre-checkout
sudo install -m 0755 post-checkout          /etc/buildkite-agent/hooks/post-checkout
sudo install -m 0755 buildkite-gpu-cdi-env.sh /usr/local/libexec/buildkite-gpu-cdi-env.sh
sudo install -m 0644 buildkite-agent.cfg    /etc/buildkite-agent/buildkite-agent.cfg
```

> **Whole-GPU machines:** the `environment` template's MIG slice-mapping block
> doesn't apply. You can either run one agent per GPU (`spawn` = number of
> GPUs) and pin by GPU index/UUID, or run agents that share all GPUs and drop
> the pinning logic. The rest of the hook (secrets, ECR login, serialized image
> pull, shallow git) applies either way.

Now edit **two** files to match your machine:

**`/etc/buildkite-agent/buildkite-agent.cfg`:**
- `token=` — your Buildkite agent registration token
- `name="my-host-%spawn"` — your hostname; `%spawn` becomes the agent number
- `tags="queue=<your-queue>"` — the queue for this machine (e.g. `h200_35gb`)
- `spawn=` — number of agents. Whole-GPU: usually the GPU count. MIG: `num_gpus
  × slices_per_gpu` (32 for 35gb, 56 for 18gb).

**`/etc/buildkite-agent/hooks/environment`:**
- Fill in the secrets: `HF_TOKEN`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
  and optionally `BUILDKITE_ANALYTICS_TOKEN`
- `HF_HOME=...` — point at large storage (see below)
- (MIG only) `SLICES_PER_GPU=` — must match the profile (4 for 35gb, 7 for 18gb)

> **HF_HOME:** the docker plugin passes `HF_HOME` through to containers and
> mounts a fixed path. Set `HF_HOME` to a path that's actually mounted into the
> container, on a big disk (4+ TB preferred). See `../AGENT.md` step 6 for a
> snippet that auto-detects the best disk.

### How device pinning works (the part worth understanding)

The agent spawns N copies of itself, named `<host>-1` … `<host>-N`. When each
job starts, the `environment` hook reads the agent number from
`BUILDKITE_AGENT_NAME`, picks that device (MIG slice UUID, or GPU), and exports
it as a CDI device so the job's container lands on exactly that device. It also
uses an `flock` so the first image pull happens once, not N times at once.

This is why `spawn`, the slice math, and the actual device count must agree —
if they don't, an agent maps to a nonexistent device or two agents collide.

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

- [ ] `nvidia-smi` lists the expected GPUs (and MIG slices, if slicing)
- [ ] `nvidia-ctk cdi list` shows the same devices
- [ ] `systemctl is-active buildkite-agent` is `active`
- [ ] N agents visible in the Buildkite dashboard on the right queue
- [ ] A test job on the queue runs and lands on the right GPU/slice
- [ ] Host appears at <https://ci.vllm.ai/gpu>

## Troubleshooting

- **Agent error: "Agent number N exceeds available MIG devices"** — `spawn` is
  larger than the actual slice count. Recreate the slices (Step 1) or lower
  `spawn`.
- **Job error: "CDI device ... is absent"** — the CDI spec is stale. Re-run
  `nvidia-ctk cdi generate` (Step 2).
- **Two jobs landing on the same device** — the slice math (`SLICES_PER_GPU`)
  doesn't match reality, or two agents share a name. Fix and restart the agent.
- **No GPU stats on the dashboard** — check
  `journalctl -u gpu-reporter.service` for a non-`OK` line; usually a wrong
  `GPU_REPORT_URL` or `GPU_REPORT_SECRET`.
- **Tear down MIG:** `sudo ../teardown_mig_h200.sh` destroys all slices and
  disables MIG (a reboot may be needed).

## Related files

- `../AGENT.md` — generic machine → Buildkite agent onboarding (OS, Docker, AWS)
- `../setup_mig_h200.sh` / `../teardown_mig_h200.sh` — MIG slice create/destroy
- `./` (`buildkite-agent/`) — agent config + hook templates
- `../gpu-reporter/` — GPU stats reporter for ci.vllm.ai/gpu
