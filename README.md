# vLLM CI Infrastructure

This repository turns vLLM's test definitions into runnable [Buildkite](https://buildkite.com/vllm) pipelines. It contains pipeline generation, change-based test selection, shared build helpers, agent image recipes, and CI automations.

Start with the [pipeline generator](buildkite/pipeline_generator/README.md) to change how jobs run, or [how test selection works](buildkite/ci_selector/HOW_IT_WORKS.md) to understand why a job was selected.

## What lives where

| Repository | Responsibility |
| --- | --- |
| [vllm](https://github.com/vllm-project/vllm) | Test code, Dockerfiles, and `.buildkite/` job definitions and bootstrap entry points. |
| **ci-infra** (this repository) | Generate and select jobs, route them to workers, provide shared CI tooling, and build agent images. |
| [ci-infra-private](https://github.com/vllm-project/ci-infra-private) | Infrastructure provisioning and state management, including the migrated `terraform/` and `github/` runner configuration. Maintainer access is required. |

Changing a test command usually starts in vLLM. Changing selection or the way that command is wrapped and dispatched usually starts here. Changing deployed infrastructure belongs in the private repository.

## How a build runs

1. **Bootstrap:** a vLLM build loads CI tooling from this repository. `VLLM_CI_BRANCH` selects the ci-infra revision; the default is `main`.
2. **Generate:** the Python pipeline generator reads vLLM's `.buildkite/ci_config.yaml` and job definitions, including image builds and test areas.
3. **Select:** the generator combines build policy with change-based test selection, keeping the selected jobs' prerequisites. It writes the generated pipeline and a selection decision artifact.
4. **Dispatch:** Buildkite schedules jobs on the configured queues. The generator supplies the Docker or Kubernetes configuration, hardware requests, environment, and dependencies.
5. **Inspect or expand:** logs and artifacts explain the result. The **Run more jobs** input can add deferred jobs and their dependencies to the same build.

The standalone AMD pipeline also has a [bootstrap script](buildkite/bootstrap-amd.sh) and [Jinja template](buildkite/test-template-amd.j2). Its behavior is separate from the Python generator's native AMD jobs and AMD mirrors; check the entry point for the pipeline you are changing.

### Test selection

The selector connects changed code to tests using a code map, build dependencies, and recorded runtime coverage. Ordinary PR, branch, and main builds use it; main builds compare against their parent commit. Uncertainty keeps broader coverage.

There are deliberate exceptions: release, nightly, run-all, and explicitly selected builds keep the generator's own rules. AMD jobs and mirrors retain their `source_file_dependencies` rules. If the selector fails, times out, or returns unknown step keys, the generator falls back to its existing rules.

Read the [selector guide](buildkite/ci_selector/README.md) for configuration, recorders, local queries, and validation. For timing-based pytest sharding, see [automatic sharding](buildkite/README.md).

## Repository map

| Path | Contents |
| --- | --- |
| [buildkite/pipeline_generator/](buildkite/pipeline_generator/) | Pipeline generation, queue routing, job wrapping, dependencies, and selection integration. |
| [buildkite/ci_selector/](buildkite/ci_selector/) | Code maps, test selection, runtime recorders, and selection validation. |
| [buildkite/tests/](buildkite/tests/) | Pipeline generator regression tests. |
| [buildkite/](buildkite/) | AMD and Intel bootstrap scripts, AMD template, and shared helpers. |
| [docker/ci.hcl](docker/ci.hcl) | Shared Docker Buildx Bake configuration. |
| [packer/](packer/) | CPU and GPU agent AMI recipes. |
| [.buildkite/pipelines/](.buildkite/pipelines/) | Infrastructure maintenance pipelines, including CPU AMI rebuilds. |
| [infra-k8s/](infra-k8s/) | Kubernetes agent installation, priorities, and secret provisioning helpers. |
| [test-selection/](test-selection/) | Confirmed selection leaks used for regression replay. |
| [claude-skills/AUTOMATIONS.md](claude-skills/AUTOMATIONS.md) | Automation inventory, service installation, schedules, state, and failover procedures. |
| [usage-stats/](usage-stats/) | Usage telemetry collection. |

## Develop and validate a change

### Pipeline generator

From the repository root, use Python 3.12 to match the test workflow:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m pip install pytest ./buildkite/pipeline_generator
python -m pytest buildkite/tests
```

The [generator reference](buildkite/pipeline_generator/README.md) documents its CLI, input schema, job types, and environment variables. The [GitHub Actions workflow](.github/workflows/pipeline-generator-tests.yml) defines the CI test command.

### Test selector

The selector has its own uv project. From the repository root:

```bash
cd buildkite/ci_selector
uv sync --locked
uv run pytest tests -q
```

By default, the tests use a cached vLLM checkout at the revision in [`VLLM_PIN`](buildkite/ci_selector/VLLM_PIN), fetching it when needed. To test against an existing checkout:

```bash
VLLM_REPO=/absolute/path/to/vllm uv run pytest tests -q
```

For selection changes, also follow the [crosscheck and leak replay instructions](buildkite/ci_selector/README.md). Unit tests alone do not establish that the intended tests are still reached. The [selector workflow](.github/workflows/ci-selector-tests.yml) includes additional recorder shell checks.

### Repository checks

From the repository root, install and run the hooks before submitting changes:

```bash
python -m pip install pre-commit
pre-commit install
pre-commit run --all-files
```

Hooks check formatting, YAML, spelling, and GitHub Actions configuration. Changes to selection regression data also trigger corpus validation.

### Validate in Buildkite

Push your ci-infra branch, then create a build against the vLLM commit you want to test. Set this build environment variable to the ci-infra branch or commit SHA:

```text
VLLM_CI_BRANCH=your-ci-infra-revision
```

Use exact commit SHAs when recording results so both the vLLM revision and the CI tooling revision are reproducible. Check the bootstrap log to confirm which tooling revision was loaded.

For Python-generated pipelines, you can limit a validation build to stable step keys and their transitive dependencies:

```text
VLLM_CI_ONLY_STEP_KEYS=["image-build"]
```

This bypasses normal change-based selection. Unknown keys fail generation. Choose keys from the vLLM job definitions, and inspect the generated pipeline before treating a passing build as validation of the intended jobs.

## Investigate CI behavior

| Question | Start here |
| --- | --- |
| Why did a job run or get skipped? | The bootstrap artifact `.buildkite/ci_selector_decision.json`: selected and scheduled keys, fallback reasons, and provenance. Compare it with the generated pipeline. |
| How do I run an omitted job? | The build's **Run more jobs** input. The generator saves deferred jobs in `deferred/deferred_steps.json` for later upload. |
| Why is an image build slow? | The image job's log: separate cache imports, compilation, image export, and registry push. Compare the actual worker queue and cache hits between builds. |
| Why did a job wait? | Buildkite scheduling timestamps and the requested agent queue; distinguish queue time from execution time. |
| Where is a hardware request or queue chosen? | The vLLM job definition and the generator's routing and job-wrapping code. |
| Where did an alert or scheduled build come from? | The [automation inventory](claude-skills/AUTOMATIONS.md), including live-service state and failover instructions. |

### Image builds and caching

Image build time depends on both the worker and the build inputs. The shared [Bake configuration](docker/ci.hcl), vLLM Dockerfiles and build scripts, registry caches, and compiler caches all contribute. A warm worker does not guarantee that a changed Dockerfile instruction can reuse its cached result.

The [CPU AMI rebuild pipeline](.buildkite/pipelines/rebuild-cpu-ami.yml) prepares workers with cached build data using the [CPU Packer recipe](packer/cpu/). The CUDA `image-build` step on non-main branches uses `cpu_queue_premerge_us_east_1`, sharing the warm-cache AMI and cache region used by main. Other non-main x86 image steps use `medium_cpu_queue_premerge`.

For current routing, inspect the generator rather than assuming every image job uses the same worker. For AMI rollout details, inspect the maintenance pipeline and infrastructure configuration rather than relying on a fixed fleet size or schedule in this README.

## Infrastructure and operations

Agent image recipes and Kubernetes installation helpers remain public here. Infrastructure provisioning and state management live in [ci-infra-private](https://github.com/vllm-project/ci-infra-private). Coordinate queue changes with both the generator and the deployed agent configuration.

For access or runner onboarding, contact the maintainers in `#sig-ci` on [vLLM Slack](https://vllm-dev.slack.com). Use the [automation operations guide](claude-skills/AUTOMATIONS.md) when installing or moving a scheduled service; merging its source does not move a running service.

## License

[Apache License 2.0](LICENSE).
