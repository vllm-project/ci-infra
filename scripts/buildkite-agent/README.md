# Buildkite Agent Templates (GPU machines)

> **New here?** Start with the step-by-step [RUNBOOK.md](RUNBOOK.md) — it walks
> through setting up a GPU machine (whole-GPU or MIG-sliced), hooking into
> Buildkite, and GPU monitoring end to end. This README is the reference for
> the template files.

Reusable, working copies of the Buildkite agent config and hooks from live
H200 MIG agents (`h200_18gb` and `h200_35gb`). Use these when onboarding a new
GPU machine so you don't have to reverse-engineer a running agent each time.

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
# On the target machine, after scripts/AGENTS.md steps 1-5 are done:
sudo install -m 0755 environment            /etc/buildkite-agent/hooks/environment
sudo install -m 0755 pre-checkout           /etc/buildkite-agent/hooks/pre-checkout
sudo install -m 0755 post-checkout          /etc/buildkite-agent/hooks/post-checkout
sudo install -d -m 0755 /usr/local/libexec
sudo install -m 0755 buildkite-gpu-cdi-env.sh /usr/local/libexec/buildkite-gpu-cdi-env.sh
sudo install -m 0644 buildkite-agent.cfg    /etc/buildkite-agent/buildkite-agent.cfg

# Edit the two files to fill in secrets / token / hostname, set spawn and
# SLICES_PER_GPU for your MIG profile, then:
sudo systemctl enable --now buildkite-agent
```

## Notes

- **Exit-code convention in the hooks.** Exit 1 means a genuine
  misconfiguration (wrong `spawn` count, stale CDI spec, invalid git config
  state) and fails the job immediately. Exit 255 means a provider /
  infrastructure failure — AWS CLI or ECR unreachable, registry or secret-store
  blip, `docker pull` failure, `nvidia-smi` hiccup. The agent preserves a
  hook's exit code as the job's exit status, and the generated pipeline steps
  automatically retry `exit_status: 255` (alongside `exit_status: -1`,
  agent-lost), so one network blip can't kill a whole build. Keep every
  provider call behind an explicit `exit 255` guard;
  never let it fall through to the default exit 1.
  Hooks are re-read at every job start, but merging a template change here does
  not update running hosts — re-install the hook on each affected agent with
  the Quick install commands above.
- `HF_HOME=/mnt/vllm-ci` matches the `h200_18gb` / `h200_35gb` docker plugins,
  which mount `/mnt/vllm-ci` into containers. Keep them in sync: if the host's
  HF cache lives elsewhere, either mount it at `/mnt/vllm-ci` or update the
  plugin's volume list.
- `spawn` must equal `num_gpus × slices_per_gpu` for the MIG-slice mapping to
  line up with the agent names (`<host>-1` … `<host>-N`).
- See [`../AGENTS.md`](../AGENTS.md) for the full machine-onboarding runbook.

### Manual fork builds and Git mirrors

For builds using an `owner:branch` name without pull-request metadata,
`pre-checkout` sets `BUILDKITE_REFSPEC` to the full commit SHA. This prevents
Git mirrors from interpreting the display name as a fetch refspec. Existing
custom refspecs and PR builds retain their normal checkout behavior. The SHA
must be fetchable from the pipeline repository (for example, through a PR).

Deploy the updated `pre-checkout` hook to affected agents using the install
command above; merging this template change does not update running hosts.

Run the hook regression checks with:

```bash
bash scripts/buildkite-agent/tests/pre-checkout.sh
```
