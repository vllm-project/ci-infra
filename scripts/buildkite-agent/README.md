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
  state) and fails the job immediately. A provider / infrastructure call that
  fails every in-hook retry exits with its own code, so Buildkite shows which
  call failed:

  | Code | Hook | Call |
  |---|---|---|
  | 201 | `pre-checkout` | GitHub token from the agent secret store |
  | 202 | `environment` | ECR public login |
  | 203 | `environment` | ECR pull-through-cache login |
  | 204 | `environment` | GPU discovery (`nvidia-smi -L`) |
  | 205 | `environment` | `docker pull` |

  The agent preserves a hook's exit code as the job's exit status, and the
  generated pipeline steps retry 201-205 once (alongside -1 agent-lost, 255
  agent-terminated and 94 Git mirror lock), so one network blip can't kill a
  whole build. Buildkite's agent reserves 0, 1, 94, 255, -1 and 128 + a signal
  number (https://buildkite.com/docs/agent/lifecycle#exit-codes), and no
  test exited in 201-205 over three days of CI failures; keep new codes there. Every
  provider call retries in place first (5 attempts, sleeping 2/4/8/16 s, then
  its code), written inline in each hook. Keep every provider call behind that
  loop; never let it fall through to the default exit 1. Each retry that fires
  also writes the build meta-data key `infra-retry:<job id>:<class>`
  (`github-token`, `ecr-public-login`, `ecr-cache-login`, `gpu-discovery`,
  `image-pull`; value
  `<class>,<attempts>,<recovered|exhausted>`); the write is capped at 10 s and
  never fails the job.
  Hooks are re-read at every job start, but merging a template change here does
  not update running hosts — re-install the hook on each affected agent with
  the Quick install commands above.
- **DNS on mithril H200 VMs.** DHCP hands out a single resolver (the
  provider's CoreDNS, `10.96.0.10`), so a CoreDNS blip fails every lookup on
  the host (`lookup ... on 127.0.0.53:53: server misbehaving`). Add public
  resolvers so systemd-resolved can answer from either:
  `printf '[Resolve]\nDNS=1.1.1.1 8.8.8.8\n' | sudo tee /etc/systemd/resolved.conf.d/10-public-dns.conf && sudo systemctl restart systemd-resolved`
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
bash scripts/buildkite-agent/tests/environment.sh   # bash 4+ and GNU grep (agent host or Linux)
bash scripts/buildkite-agent/tests/environment-race.sh   # real flock (agent host or Linux)
```

None needs AWS credentials or a GPU: they stub every call that leaves the host.
