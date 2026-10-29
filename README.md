# vLLM CI Infrastructure

Bootstrap scripts, pipeline generation, and test selection for vLLM's continuous integration pipeline, built on [Buildkite](https://buildkite.com/). Infrastructure configuration is maintained in [ci-infra-private](https://github.com/vllm-project/ci-infra-private) (maintainer access required).

## Repository Structure

```
ci-infra/
├── .buildkite/            # Scheduled Buildkite pipelines (e.g. daily AMI rebuild)
├── buildkite/             # Bootstrap scripts, pipeline generation, and build helpers
│   ├── bootstrap-amd.sh   # AMD/ROCm CI entry point
│   ├── bootstrap-intel.sh # Intel CI entry point
│   ├── pipeline_generator/  # Python-based pipeline generator
│   ├── test-template-amd.j2  # AMD/ROCm Jinja2 pipeline template
│   └── scripts/           # Helper scripts (Docker bake, Codecov upload)
├── docker/                # Docker buildx bake configuration (ci.hcl)
├── packer/
│   ├── cpu/               # CPU build AMI with warm Docker cache
│   └── gpu/               # GPU AMI with NVIDIA drivers
├── infra-k8s/             # Kubernetes-based Buildkite agent deployment
├── claude-skills/         # Portable CI automations and investigation skills
└── usage-stats/           # Usage telemetry collection (Vector)
```

## CI Automations and Alerts

See [claude-skills/AUTOMATIONS.md](claude-skills/AUTOMATIONS.md) for the
source-of-truth inventory of:

- portable scheduled automations and their destinations;
- systemd services, timers, schedules, credentials, and persistent state;
- installation, failover, validation, and operational commands;
- host-local automations that are not portable yet;
- investigation skills that do not run on a schedule.

## How CI Works

When a commit is pushed to the [vLLM repository](https://github.com/vllm-project/vllm), a Buildkite webhook triggers a new build. The pipeline is dynamically generated based on the changes in the commit.

### Build Flow

```
GitHub push/PR
    │
    ▼
Buildkite webhook triggers build
    │
    ▼
Bootstrap step
    ├── Installs pipeline-generator from ci-infra
    ├── Generates pipeline YAML from .buildkite/ci_config.yaml
    └── Uploads pipeline YAML to Buildkite
    │
    ▼
Steps dispatched to agent queues
    ├── CPU queues ─── Docker image build, compilation, CPU tests
    ├── GPU queues ─── Single/multi-GPU tests (L4, A100, H100, H200, B200)
    ├── TPU queues ─── TPU-specific tests (v5, v6e, v7x)
    └── AMD queues ─── ROCm tests (MI250, MI325, MI355)
    │
    ▼
Results reported back (test results, coverage via Codecov)
```

### Pipeline Generation

#### Pipeline Generator (primary — CUDA/CPU CI, Intel CI)

A Python tool (`buildkite/pipeline_generator/`) that reads step definitions from YAML files in the vLLM repo (e.g. `.buildkite/test_areas/`, `.buildkite/image_build/`), groups them, and converts them into Buildkite YAML with proper agent queues, Docker/Kubernetes plugins, and environment configuration.

**How it runs:** The bootstrap step installs `pipeline-generator` from ci-infra (`pip install git+...ci-infra.git@main#subdirectory=buildkite/pipeline_generator`) and invokes it with the vLLM repo's `.buildkite/ci_config.yaml`. The `VLLM_CI_BRANCH` environment variable controls which ci-infra branch to install from (defaults to `main`).

**Config:** Driven by a YAML config file (`ci_config.yaml`) that specifies job directories, registries, repositories, and `run_all_patterns`. See the [pipeline generator README](buildkite/pipeline_generator/README.md) for details.

**Features:**
- **Device types**: CPU, GPU (L4), A100, H100, H200, H200 18GB (MIG), B200, GH200, Intel CPU/HPU/GPU, ARM CPU, Ascend NPU, AMD MI250/MI325/MI355 (1-8 devices)
- **Plugin generation**: Docker plugin for EC2-based queues, Kubernetes pod specs for H100/H200/A100
- **AMD mirroring**: Steps can define `mirror.amd` to automatically create parallel AMD test runs
- **Source file dependencies**: Steps can specify which source files they depend on for intelligent test filtering
- **Block steps**: Optional tests are gated behind manual approval blocks
- **Variable injection**: Automatically injects registry URLs, cache tags, and image references into step commands

#### Jinja2 Template (AMD CI)

`buildkite/test-template-amd.j2` renders vLLM's [`test-amd.yaml`](https://github.com/vllm-project/vllm/blob/main/.buildkite/test-amd.yaml) into Buildkite YAML using [minijinja-cli](https://github.com/mitsuhiko/minijinja).

When the vLLM checkout has `.buildkite/scripts/rocm/build.sh`, the ROCm build steps come from vLLM's `.buildkite/hardware_tests/amd.yaml` (the template and `amd.py` alike) and GPU pods run `$ROCM_CI_REPO:build-$BUILDKITE_BUILD_ID`. `ROCM_CI_REPO` is the trust decision: `rocm/vllm-ci` for non-PR builds of `vllm-project/vllm` `main` that are not the AITER nightly, else `rocm/vllm-dev`. Older checkouts keep the previous build chain.

ROCm release images are built in vLLM's release pipeline itself, on its own `cpu_queue_release` agents (`build.sh rocm release`, which builds its own stack and runtime images from source with the CI cache as a build cache, and never ships images from `rocm/vllm-ci`); no AMD agent or pipeline from this repo takes part.

#### AITER nightly (AMD CI)

`AITER_NIGHTLY=1`, on a scheduled `amd-ci` build of `main`, tests last night's [AITER](https://github.com/ROCm/aiter) build against vLLM. Every `amdproduction` step on MI300 or MI355 (`amdgfx942nightly`, `amdgfx950nightly`) runs, unblocked. MI250 is left out: AITER builds no kernels for it.

With `build.sh`, `image-build-amd` installs the newest AITER nightly wheel built for the image's ROCm, torch and Python into the test image (selected by vLLM's `select_aiter_nightly_wheel.py`; an `AITER_SPEC` in the build env pins a pip spec instead), and the build writes only to `rocm/vllm-dev`. Older checkouts (before the build.sh layout) instead run:

1. `aiter-nightly-amd` (vLLM's [`.buildkite/scripts/rocm/aiter-nightly-overlay.sh`](https://github.com/vllm-project/vllm/blob/fc0cfcdc05/.buildkite/scripts/rocm/aiter-nightly-overlay.sh)) installs the newest AITER nightly wheel built for the image's ROCm, torch and Python over this build's ci_base, and pushes the result as this build's `rocm/vllm-dev:ci_base-build-$BUILDKITE_BUILD_ID`, in place of the stock one.
2. The test image and every native GPU job run on that image. Test image layers go to an `aiter-nightly` cache branch.

With `build.sh`, a failed selection or install fails `image-build-amd` (retried once as exit 1); the wheel and its build date are in the build log. In older checkouts the overlay step fails before any GPU job runs, with an error annotation and exit code: `10` no wheel for the image's ROCm/torch/Python, `11` install failed. The newest wheel is tested however old it is; the annotation shows its build date.

A retried overlay step reinstalls the wheel its first attempt chose.

### Bootstrap Scripts

| Script | Pipeline | Generation Method |
|--------|----------|-------------------|
| `bootstrap-intel.sh` | Intel CI | Pipeline generator (via ci-infra) |
| `bootstrap-amd.sh` | AMD/ROCm CI | Jinja2 template (`test-template-amd.j2`) |

The AMD and Intel bootstrap scripts additionally handle:
- **Diff detection**: Computes changed files vs. `origin/main` (PRs) or `HEAD~1` (main branch).
- **Docs-only skip**: If all changes are in `docs/`, `*.md`, or `mkdocs.yaml`, CI is skipped entirely.
- **Run-all detection**: Changes to critical files (`Dockerfile`, `CMakeLists.txt`, `csrc/`, `setup.py`, `requirements/*.txt`) trigger all tests and build wheels from source.
- **Precompiled wheels**: When no critical files changed, precompiled wheels from the merge-base commit are used if available at `wheels.vllm.ai`.
- **PR labels**: `ready-run-all-tests` forces all tests including optional/nightly. `ci-no-fail-fast` disables fail-fast mode.
- **ECR cache resolution**: Resolves multi-layer cache sources with a fallback chain (PR-specific -> base branch -> main).

## Infrastructure

Terraform configuration for AWS, GCP, TPU/Kubernetes infrastructure, and GitHub runner groups is maintained in [ci-infra-private](https://github.com/vllm-project/ci-infra-private). The original `terraform/` and `github/` directory layouts are preserved there. Infrastructure maintainers need access to that repository to plan or update deployments.

Public pipeline generation and test selection remain here. Infrastructure provisioning and state management are handled from the private repository.

### Kubernetes

The `infra-k8s/` directory provides Helm-based deployment of the [Buildkite Agent Stack for Kubernetes](https://github.com/buildkite/agent-stack-k8s). Used for specialized hardware (A100, H100, H200, B200) where the pipeline generator creates Kubernetes pod specs with GPU resource requests, node selectors, and shared storage mounts (EFS/FSx for model weights).

## Caching Strategy

Build performance relies on a multi-layer caching approach:

### Docker Layer Cache (BuildKit + ECR)

The bootstrap script resolves cache sources with a fallback chain:

1. **PR-specific cache**: `vllm-ci-test-cache:pr-{number}`
2. **Base branch cache**: Cache from the PR's target branch
3. **Main branch cache**: `vllm-ci-postmerge-cache:latest`

Cache is stored as registry refs in private ECR repositories with 14-day expiration.

### Warm-Cache AMI

A daily Buildkite pipeline (`.buildkite/pipelines/rebuild-cpu-ami.yml`, scheduled at 3 AM PST) builds custom EC2 AMIs with pre-warmed Docker layer caches:

1. **Build AMI**: Packer creates an AMI from the Buildkite Elastic Stack base, installs BuildKit as a standalone systemd service (not docker-container driver), and pre-pulls the latest postmerge Docker image layers using the bake config artifact from the most recent CI build.
2. **Update SSM**: Stores the new AMI ID in AWS SSM Parameter Store.
3. **Update Launch Templates**: Creates new EC2 Launch Template versions for premerge, postmerge, and release CPU queues, then updates Auto Scaling Groups. Existing instances cycle out naturally (`TerminateInstanceAfterJob=true`).
4. **Cleanup**: Retains the 3 most recent old AMIs for rollback, deletes older ones and their snapshots.

The warm-cache AMI is used by the `cpu_queue_*_us_east_1` queues and release queues.

### sccache

C++ compilation outputs are cached in S3 (`vllm-build-sccache` in us-west-2), configured via `docker/ci.hcl`.

### Precompiled Wheels

When no critical source files have changed, CI uses precompiled wheels from the merge-base commit (hosted at `wheels.vllm.ai`) instead of building from source.

## Development

### Testing Changes

1. Create a feature branch on this repo (contact @khluu for access if needed).
2. Push your changes to the branch.
3. Create a new build on Buildkite with the environment variable:
   ```
   VLLM_CI_BRANCH=my-feature-branch
   ```
   This tells the bootstrap script to fetch templates/code from your branch instead of `main`.

**Notes:**
- Run builds on your own vLLM feature branch/fork, preferably up-to-date with `main`.
- For fork branches, use the full commit hash (not `HEAD`) and format the branch name as `<fork/username>:<branch>`.

### Key Environment Variables

| Variable | Description |
|----------|-------------|
| `VLLM_CI_BRANCH` | ci-infra branch to use for templates (default: `main`) |
| `RUN_ALL` | Force all tests to run |
| `SKIP_TIMEOUT` | Omit configured step timeouts from Python-generated pipelines when set to `1` |
| `NIGHTLY` | Include optional nightly tests |
| `VLLM_USE_PRECOMPILED` | Use precompiled wheels (`1`) or build from source (`0`) |
| `COV_ENABLED` | Enable pytest coverage collection and Codecov upload |
| `DOCS_ONLY_DISABLE` | Skip docs-only detection (always run CI) |
| `AMD_MIRROR_HW` | AMD hardware mirror target (default: `amdproduction`) |
| `AITER_NIGHTLY` | AMD CI: set to `1` to test the latest AITER nightly wheel on every MI300/MI355 production step (see [AITER nightly](#aiter-nightly-amd-ci)); also disables the docs-only skip |
| `AITER_SPEC` | AMD CI, with vLLM's `build.sh`: optional pip spec that pins the AITER wheel the test image installs (default with `AITER_NIGHTLY=1`: the newest matching nightly) |
| `NOAUTO` | Set to `1` to gate all steps behind manual approval blocks |
| `PRIORITY` | Set to `HIGH` for high-priority pipeline scheduling |

### Pre-commit Hooks

```bash
pip install pre-commit
pre-commit install
```

Enforces YAML validation, Python formatting (ruff), spell checking (typos), and GitHub Actions linting (actionlint).

## Onboarding New Runners

Machines communicate with Buildkite via an agent. Three deployment options:

1. **[Elastic CI Stack](https://buildkite.com/docs/agent/v3/elastic-ci-aws/elastic-ci-stack-overview)**: Autoscaling EC2 instances (used by AWS queues).
2. **[K8s Agent Stack](https://github.com/buildkite/agent-stack-k8s)**: Kubernetes-orchestrated agents (used for specialized GPU hardware).
3. **[Standalone Agent](https://buildkite.com/docs/agent/v3)**: Direct installation on existing machines.

All options require a Buildkite agent token and queue name. Contact @khluu on `#sig-ci` ([vllm-dev.slack.com](https://vllm-dev.slack.com)) to obtain credentials.

For standalone agents, add the token and queue to the agent config (typically `/etc/buildkite-agent/buildkite-agent.cfg`) and restart the service.

## License

Apache License 2.0
