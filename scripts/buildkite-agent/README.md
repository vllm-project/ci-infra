# Buildkite Agent Templates (H200 MIG)

Reusable, working copies of the Buildkite agent config and hooks from live
H200 MIG agents (`h200_18gb` and `h200_35gb`). Use these when onboarding a new
H200 MIG machine so you don't have to reverse-engineer a running agent each time.

Secrets are replaced with `<placeholders>` — fill them in on the target machine.
Nothing secret is committed here.

## MIG profiles and agent counts (8-GPU H200)

One Buildkite agent is spawned per MIG slice, so `spawn` = slices per GPU × 8:

| Profile | Slices/GPU | Agents (spawn) | Queue | `SLICES_PER_GPU` |
|---------|-----------:|---------------:|-------|------------------|
| `1g.18gb` | 7 | 56 | `h200_18gb` | 7 |
| `1g.35gb` | 4 | 32 | `h200_35gb` | 4 |

The `environment` hook maps each agent's spawn number to a MIG slice UUID using
`SLICES_PER_GPU` (set near the top of that file). Keep three things in sync:
the MIG profile created on the GPUs, `spawn` in `buildkite-agent.cfg`, and
`SLICES_PER_GPU` in `environment`.

## Files

| File | Install to | Purpose |
|------|-----------|---------|
| `buildkite-agent.cfg` | `/etc/buildkite-agent/buildkite-agent.cfg` | Agent config: queue tag, `spawn` (see table above), build/git-mirror paths. |
| `environment` | `/etc/buildkite-agent/hooks/environment` | Exports secrets, logs into ECR, maps agent spawn # → MIG slice UUID, serializes the first docker pull, sets shallow git flags. |
| `buildkite-gpu-cdi-env.sh` | `/usr/local/libexec/buildkite-gpu-cdi-env.sh` | Converts the MIG GPU selection into a CDI device (`nvidia.com/gpu=...`) and validates it exists. Sourced by `environment`. |
| `pre-checkout` | `/etc/buildkite-agent/hooks/pre-checkout` | Injects a GitHub read-only PAT (from the agent secret store) as a git HTTP header for the vllm / perf-eval repos. |
| `post-checkout` | `/etc/buildkite-agent/hooks/post-checkout` | Removes the injected git header after fetch. |

## Secrets to fill in

In `environment`:
- `HF_TOKEN` — Hugging Face token (required for gated models / rate limits).
- `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` — for ECR image pulls.
- `BUILDKITE_ANALYTICS_TOKEN` — optional, Buildkite Test Analytics.

In `buildkite-agent.cfg`:
- `token` — the Buildkite agent registration token for the target cluster.

The GitHub PAT is **not** in a file — `pre-checkout` fetches it at runtime via
`buildkite-agent secret get GITHUB_READONLY_PAT`, so it must exist in the
cluster's secret store.

## Quick install

```bash
# On the target machine, after scripts/AGENT.md steps 1-5 are done:
sudo install -m 0755 environment            /etc/buildkite-agent/hooks/environment
sudo install -m 0755 pre-checkout           /etc/buildkite-agent/hooks/pre-checkout
sudo install -m 0755 post-checkout          /etc/buildkite-agent/hooks/post-checkout
sudo install -m 0755 buildkite-gpu-cdi-env.sh /usr/local/libexec/buildkite-gpu-cdi-env.sh
sudo install -m 0644 buildkite-agent.cfg    /etc/buildkite-agent/buildkite-agent.cfg

# Edit the two files to fill in secrets / token / hostname, set spawn and
# SLICES_PER_GPU for your MIG profile, then:
sudo systemctl enable --now buildkite-agent
```

## Notes

- `HF_HOME=/mnt/vllm-ci` matches the `h200_18gb` / `h200_35gb` docker plugins,
  which mount `/mnt/vllm-ci` into containers. Keep them in sync: if the host's
  HF cache lives elsewhere, either mount it at `/mnt/vllm-ci` or update the
  plugin's volume list.
- `spawn` must equal `num_gpus × slices_per_gpu` for the MIG-slice mapping to
  line up with the agent names (`<host>-1` … `<host>-N`).
- See [`../AGENT.md`](../AGENT.md) for the full machine-onboarding runbook.
