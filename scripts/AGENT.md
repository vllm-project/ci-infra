# Preparing a Machine as a Buildkite Agent

Step-by-step guide for setting up a bare machine (physical or VM) to run Buildkite CI jobs.

## Prerequisites

- A Linux machine (Ubuntu 20.04+ or Amazon Linux 2/2023) with root/sudo access
- Network access to the internet (for package installs and Buildkite registration)
- A Buildkite agent token (from your org's Buildkite dashboard under Agents → Reveal Agent Token)

## 1. Install Docker

```bash
# Ubuntu / Debian
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin

# Amazon Linux 2023
sudo dnf install -y docker
sudo systemctl enable --now docker
```

Verify:

```bash
docker --version
sudo systemctl is-active docker
```

## 2. Install AWS CLI

```bash
# Universal installer (works on any Linux x86_64)
curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip"
unzip awscliv2.zip
sudo ./aws/install
rm -rf aws awscliv2.zip
```

Verify:

```bash
aws --version
```

Configure credentials as needed for ECR pulls or S3 access (instance profile, env vars, or `aws configure`).

## 3. Install the Buildkite Agent

```bash
# Ubuntu / Debian
sudo sh -c 'echo deb https://apt.buildkite.com/buildkite-agent stable main > /etc/apt/sources.list.d/buildkite-agent.list'
sudo apt-key adv --keyserver hkp://keyserver.ubuntu.com --recv-keys 32A37959C2FA5C3C99EFBC32A79206696452D198
sudo apt-get update
sudo apt-get install -y buildkite-agent

# Amazon Linux / RHEL
sudo sh -c 'echo -e "[buildkite-agent]\nname = Buildkite Pty Ltd\nbaseurl = https://yum.buildkite.com/buildkite-agent/stable/x86_64/\nenabled=1\ngpgcheck=0\npriority=1" > /etc/yum.repos.d/buildkite-agent.repo'
sudo yum install -y buildkite-agent
```

### Grant buildkite-agent access to Docker and AWS

```bash
# Add buildkite-agent to the docker group so it can run containers
sudo usermod -aG docker buildkite-agent

# Verify group membership (may need a new shell or reboot)
sudo -u buildkite-agent docker ps
```

If you use instance-level AWS credentials (IAM role), the buildkite-agent user inherits them automatically. For explicit credentials, set `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` in the agent's environment hook at `/etc/buildkite-agent/hooks/environment`.

> **Do not start the agent yet.** Finish all remaining setup steps first (docker/containerd data roots, GPU drivers, etc.) so the agent doesn't pick up jobs on a half-configured machine.

## 4. Move Docker and containerd Data Roots (Optional but Recommended)

By default Docker stores images/containers under `/var/lib/docker` and containerd under `/var/lib/containerd`. On machines with a small root partition and a large secondary mount (NVMe, tmpfs, etc.), move both to the larger volume.

Use the provided script:

```bash
sudo ./scripts/move-docker-containerd.sh /path/to/target
# e.g. /dev/shm for RAM-backed ephemeral storage
# e.g. /mnt/fast-nvme for a mounted NVMe drive
```

The script will:
- Set Docker's `data-root` in `/etc/docker/daemon.json`
- Set containerd's `root` in `/etc/containerd/config.toml`
- Install systemd drop-ins so the target directories are recreated on boot
- Restart both services and run a smoke test

See [`move-docker-containerd.sh`](move-docker-containerd.sh) for details.

## 5. GPU Setup (If Applicable)

For GPU machines, also install the NVIDIA driver and container toolkit:

```bash
# Install NVIDIA driver (adjust version for your GPU/CUDA needs)
# See packer/gpu/scripts/install-nvidia-docker.sh for a tested example

# Install NVIDIA container toolkit
curl -fsSL https://nvidia.github.io/libnvidia-container/stable/rpm/nvidia-container-toolkit.repo | \
  sudo tee /etc/yum.repos.d/nvidia-container-toolkit.repo    # RHEL/Amazon Linux
# or for Ubuntu:
# curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
#   sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list

sudo yum install -y nvidia-container-toolkit   # or apt-get
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

Verify:

```bash
nvidia-smi
docker run --rm --gpus all nvidia/cuda:12.8.0-base-ubuntu22.04 nvidia-smi
```

For H200 MIG setup, see [`setup_mig_h200.sh`](setup_mig_h200.sh) and [`teardown_mig_h200.sh`](teardown_mig_h200.sh).

## 6. Configure and Start the Buildkite Agent

Now that the machine is fully prepared, configure the agent and bring it online.

### Determine your token and queue

Before editing the config, decide:

1. **Which agent token?** Each Buildkite organization (or cluster) has its own token.
   Go to your Buildkite dashboard → Agents → Reveal Agent Token and copy it.
   If you have multiple clusters (e.g. `vllm-ci`, `external-contributors`), make sure
   you pick the token for the cluster this machine should join.

2. **Which queue?** The queue tag controls which pipelines can schedule work on this agent.
   Common queues in this project:
   - `queue=default` — general-purpose CPU jobs
   - `queue=gpu` — GPU test jobs (add GPU-specific tags too, e.g. `gpu=h200,gpu-count=8`)
   - `queue=small_gpu` — smaller GPU instances / MIG slices

   Check your pipeline YAML or ask the team which queue this machine should serve.

### Edit the config

```bash
sudo vim /etc/buildkite-agent/buildkite-agent.cfg
```

```ini
token="<paste your agent token here>"
name="my-machine-%spawn"
tags="queue=<your queue>,<any additional tags>"
```

### Start the agent

```bash
sudo systemctl enable --now buildkite-agent
sudo systemctl status buildkite-agent
```

The agent should appear in your Buildkite dashboard under Agents within a few seconds. Verify it shows the correct tags/queue.

## Quick Checklist

- [ ] Docker installed and running
- [ ] AWS CLI installed and credentials accessible
- [ ] Buildkite agent package installed
- [ ] `buildkite-agent` user is in the `docker` group
- [ ] Docker/containerd data roots moved if needed (step 4)
- [ ] NVIDIA driver + container toolkit installed (GPU machines only)
- [ ] Agent configured with correct token and queue tags (step 6)
- [ ] Agent started and visible in Buildkite dashboard
