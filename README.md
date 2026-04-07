# vLLM CI Infrastructure

Infrastructure-as-Code and bootstrap scripts for vLLM's continuous integration pipeline, built on [Buildkite](https://buildkite.com/) with autoscaling compute across AWS and GCP.

## Repository Structure

```
ci-infra/
├── .buildkite/            # Scheduled Buildkite pipelines (e.g. daily AMI rebuild)
├── buildkite/             # Bootstrap scripts, Jinja2 pipeline templates, and build scripts
│   ├── bootstrap.sh       # Main CI entry point (CUDA/CPU)
│   ├── bootstrap-amd.sh   # AMD/ROCm CI entry point
│   ├── test-template-ci.j2       # Full CI pipeline template
│   ├── test-template-fastcheck.j2 # Lightweight fast-check template
│   ├── test-template-amd.j2      # AMD/ROCm pipeline template
│   ├── pipeline_generator/        # Python-based pipeline generator (WIP)
│   └── scripts/           # Helper scripts (Docker bake, Codecov upload)
├── docker/                # Docker buildx bake configuration (ci.hcl)
├── terraform/
│   ├── aws/               # AWS infrastructure (primary, active)
│   ├── gcp/               # GCP GKE cluster setup
│   └── gcp_old/           # GCP TPU v5/v6e infrastructure (legacy)
├── packer/
│   ├── cpu/               # CPU build AMI with warm Docker cache
│   └── gpu/               # GPU AMI with NVIDIA drivers
├── infra-k8s/             # Kubernetes-based Buildkite agent deployment
├── github/                # GitHub Actions runner groups (Neural Magic, IBM)
└── usage-stats/           # Usage telemetry collection (Vector)
```

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
Bootstrap step (buildkite/bootstrap.sh)
    ├── Detects changed files
    ├── Determines test scope (subset vs. run-all)
    ├── Resolves Docker cache layers (ECR)
    ├── Renders Jinja2 template with vLLM's test list
    └── Uploads generated pipeline YAML to Buildkite
    │
    ▼
Steps dispatched to agent queues
    ├── CPU queues ─── Docker image build, compilation, CPU tests
    ├── GPU queues ─── Single/multi-GPU tests (L4, A100, H100, H200, B200)
    ├── TPU queues ─── TPU-specific tests
    └── AMD queues ─── ROCm tests (MI250, MI325, MI355)
    │
    ▼
Results reported back (test results, coverage via Codecov)
```

### Smart Test Selection

The bootstrap script analyzes the diff to decide what to run:

- **Docs-only changes** (`docs/`, `*.md`, `mkdocs.yaml`): CI is skipped entirely.
- **Critical file changes** (`Dockerfile`, `CMakeLists.txt`, `csrc/`, `setup.py`, `requirements/*.txt`): All tests run and wheels are built from source.
- **Other changes**: Only affected tests run, using precompiled wheels from the merge-base commit when available (fetched from `wheels.vllm.ai`).
- **PR labels**: `ready-run-all-tests` forces all tests including optional/nightly ones. `ci-no-fail-fast` disables fail-fast mode.

### Pipeline Templates

Three Jinja2 templates generate Buildkite YAML from vLLM's [`test-pipeline.yaml`](https://github.com/vllm-project/vllm/blob/main/.buildkite/test-pipeline.yaml):

| Template | Pipeline | Purpose |
|----------|----------|---------|
| `test-template-ci.j2` | `ci` | Full CI pipeline with all hardware targets |
| `test-template-fastcheck.j2` | `fastcheck` | Lightweight subset for quick validation |
| `test-template-amd.j2` | `ci-amd` | AMD/ROCm GPU pipeline |

Templates are rendered using [minijinja-cli](https://github.com/mitsuhiko/minijinja) and support per-step configuration for GPU count, hardware type, working directory, parallelism, and multi-node execution.

## Infrastructure

### AWS (Primary)

Managed via Terraform in `terraform/aws/`. Uses the [Buildkite Elastic CI Stack for AWS](https://github.com/buildkite/elastic-ci-stack-for-aws) (v6.21.0) to deploy autoscaling agent clusters as CloudFormation stacks.

**Regions**: us-west-2 (GPU + CPU queues), us-east-1 (CPU build queues with warm-cache AMI)

#### Agent Queues

| Queue | Instance Type | Max | Purpose |
|-------|--------------|-----|---------|
| `small_cpu_queue_premerge` | r6in.large | 40 | Bootstrap, docs, lightweight tasks |
| `medium_cpu_queue_premerge` | r6in.4xlarge | 40 | Medium CPU workloads |
| `cpu_queue_premerge` | r6in.16xlarge (512GB) | 10 | CUDA kernel compilation |
| `cpu_queue_premerge_us_east_1` | r6in.16xlarge (512GB) | 20 | CPU builds (warm-cache AMI) |
| `arm64_cpu_queue_premerge` | r7g.16xlarge | 10 | ARM64 builds |
| `gpu_1_queue` | g6.4xlarge (1x L4) | 208 | Single-GPU tests |
| `gpu_4_queue` | g6.12xlarge (4x L4) | 64 | Multi-GPU tests |

Equivalent postmerge and release queues exist with write access to ECR for pushing images. Specialized hardware (H100, H200, B200, A100) is managed via Kubernetes or dedicated pools.

**Each queue consists of:**
- An EC2 Auto Scaling Group that scales instances based on workload
- A Lambda function that polls Buildkite to assess capacity needs
- Buildkite agents running on each instance, executing jobs in Docker containers

#### AWS Resources

- **VPC**: Isolated networks in us-west-2 and us-east-1 (10.0.0.0/16, 4 public subnets each)
- **ECR**: `vllm-ci-test-cache` (PR builds) and `vllm-ci-postmerge-cache` (main branch) with 14-day lifecycle
- **S3**: `vllm-build-sccache` for C++ compiler cache, `vllm-wheels` / `vllm-wheels-dev` for wheel binaries
- **Secrets Manager**: HuggingFace token, Buildkite analytics token
- **SSM Parameter Store**: Agent tokens, AMI IDs

### GCP

- **GKE** (`terraform/gcp/`): Kubernetes clusters with L4 GPU node pools (g2-standard machines) for containerized agent workloads. Artifact registry with 7-day cleanup.
- **TPU** (`terraform/gcp_old/`): TPU v5 and v6e nodes for TPU CI and benchmarks, with Buildkite agents installed directly on instances.

### Kubernetes

The `infra-k8s/` directory provides Helm-based deployment of the [Buildkite Agent Stack for Kubernetes](https://github.com/buildkite/agent-stack-k8s). Used primarily for specialized hardware (A100, H100, H200, B200) where pipeline templates generate Kubernetes pod specs with GPU resource requests and shared storage mounts (EFS/FSx for model weights).

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

1. **Build AMI**: Packer creates an AMI from the Buildkite Elastic Stack base, pre-pulls the latest postmerge Docker image layers into a persistent BuildKit daemon (not docker-container driver), maximizing cache locality.
2. **Update SSM**: Stores the new AMI ID in AWS SSM Parameter Store.
3. **Update Launch Templates**: Creates new EC2 Launch Template versions and updates Auto Scaling Groups. Existing instances cycle out naturally (`TerminateInstanceAfterJob=true`).
4. **Cleanup**: Retains the 3 most recent old AMIs for rollback, deletes older ones.

The warm-cache AMI is used by the `cpu_queue_premerge_us_east_1` and `cpu_queue_postmerge_us_east_1` queues.

### sccache

C++ compilation outputs are cached in S3 (`vllm-build-sccache` in us-west-2), configured via `docker/ci.hcl`.

### Precompiled Wheels

When no critical source files have changed, the CI uses precompiled wheels from the merge-base commit (hosted at `wheels.vllm.ai`) instead of building from source.

## GitHub Runner Groups

The `github/` directory contains Terraform configurations for GitHub Actions runner groups used by partner organizations:

- **Neural Magic** (`group-neural-magic/`): Dedicated runner group for Neural Magic workloads.
- **IBM** (`group-ibm/`): Dedicated runner group for IBM workloads.

These are deployed with `terraform apply` and require a GitHub PAT with organizational admin permissions.

## Development

### Testing Changes

1. Create a feature branch on this repo (contact @khluu for access if needed).
2. Push your changes to the branch.
3. Create a new build on Buildkite with the environment variable:
   ```
   VLLM_CI_BRANCH=my-feature-branch
   ```
   This tells the bootstrap script to fetch templates from your branch instead of `main`.

**Notes:**
- Run builds on your own vLLM feature branch/fork, preferably up-to-date with `main`.
- For fork branches, use the full commit hash (not `HEAD`) and format the branch name as `<fork/username>:<branch>`.

### Key Environment Variables

| Variable | Description |
|----------|-------------|
| `VLLM_CI_BRANCH` | ci-infra branch to use for templates (default: `main`) |
| `RUN_ALL` | Force all tests to run |
| `NIGHTLY` | Include optional nightly tests |
| `VLLM_USE_PRECOMPILED` | Use precompiled wheels (`1`) or build from source (`0`) |
| `COV_ENABLED` | Enable pytest coverage collection and Codecov upload |
| `DOCS_ONLY_DISABLE` | Skip docs-only detection (always run CI) |
| `AMD_MIRROR_HW` | AMD hardware mirror target (default: `amdproduction`) |

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
