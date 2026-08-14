# vLLM Pipeline Generator

A small tool to dynamically generate Buildkite pipeline for vLLM projects, running on vLLM CI infrastructure.

## Installation

You can install it using pip:

```bash
pip install git+https://github.com/vllm-project/ci-infra.git#subdirectory=buildkite/pipeline_generator
```

## Usage

The main entry point is `pipeline-generator`. It requires 2 args:
1. Path to your CI configuration file an output path.
2. Path to the output Buildkite-formatted yaml file.

```bash
pipeline-generator --pipeline_config_path <config.yaml> --output_file_path <output.yaml>
```

### Example

```bash
pipeline-generator --pipeline_config_path pipeline_config.yaml --output_file_path pipeline.yaml
```

## Configuration File Format

The configuration file is a YAML file that defines how the pipeline should be generated.

```yaml
# Name of the pipeline (e.g., vllm_ci)
name: vllm_ci

# List of directories containing step definitions (YAML files)
job_dirs:
  - ".buildkite/test_areas"
  - ".buildkite/image_build"

# List of regex patterns to trigger all tests. If any changed file matches these,
# all steps will be marked to run (overriding individual file dependencies).
run_all_patterns:
  - "docker/Dockerfile"
  - "CMakeLists.txt"
  - "requirements/common.txt"
  - "setup.py"
  - "csrc/"

# List of regex patterns to exclude from run_all_patterns checks.
# If a file matches run_all_patterns but ALSO matches one of these,
# it will NOT trigger a "run all".
run_all_exclude_patterns:
  - "docker/Dockerfile."
  - "csrc/cpu/"

# Container registry to store images
registries: public.ecr.aws/q9t5s3a7

# Repository names for different stages
repositories:
  main: "vllm-ci-postmerge-repo"    # Used for main branch builds
  premerge: "vllm-ci-test-repo"     # Used for PR/pre-merge builds

# Capability gate for the narrow AMD HF cache retry cohort.
amd_hf_offline_retry: false
```

### AMD Hugging Face Offline Retry

This policy is off by default. A pipeline must set the strict boolean
`amd_hf_offline_retry: true`, and each selected direct AMD step must also set
`hf_offline_retry: true` (or `mirror.amd.hf_offline_retry: true` for a mirror).
Only single-node jobs using `run-amd-test.sh` are eligible; direct-command
(`no_plugin`) and multi-node jobs remain disabled.

On the first presubmit attempt, the vLLM runner sets the Hugging Face Hub and
Transformers cache-only flags. Ordinary `main`/postmerge builds and scheduled
`NIGHTLY=1` and `TORCH_NIGHTLY=1` attempts start online so their caches can
refresh. This does not isolate the job's network or block direct HTTP and other
clients. Exit status `1` triggers the intended Buildkite fallback in a fresh
job. Conservatively, any retry count greater than zero (including a manual,
infrastructure, or other automatic retry) lets those Hugging Face clients use
the network. Statuses `2` and `123` are not retry signals for this policy. At
generation time, the pipeline emits the resolved
`VLLM_CI_HF_OFFLINE_RETRY=1` or `0` on every wrapper-backed AMD job.

Set `VLLM_CI_DISABLE_HF_OFFLINE_RETRY=1` to disable the cohort in newly
generated pipelines. The vLLM runner also reads this switch at job start, so a
runtime agent or repository hook can disable the client-mode override for
queued or newly started jobs whose pipeline was already generated. A runtime
switch cannot remove an exit-status retry already serialized into that
pipeline. Regenerating a legacy Jinja pipeline with the switch enabled omits
this policy's conditional exit-status-`1` retry. Python-generated AMD jobs keep
their longstanding generic exit-status-`1` retry for backward compatibility
even when this policy is disabled; the switch still prevents Hugging Face
client-mode switching. The runner clears the variable before running commands.
The switch cannot change a command that is already running. It accepts only
`0` or `1`; invalid values stop pipeline generation or job startup.

## Environment Variables

The generator relies on several environment variables, typically provided by Buildkite or set by user:

*   `BUILDKITE_BRANCH`: Current branch name.
*   `BUILDKITE_COMMIT`: Current commit hash.
*   `BUILDKITE_PULL_REQUEST`: Pull request number (or "false").
*   `BUILDKITE_PULL_REQUEST_BASE_BRANCH`: Base branch for PRs.
*   `NIGHTLY`: Set to "1" to auto-run the curated torch-nightly steps (those tagged `mirror.torch_nightly`).
*   `TORCH_NIGHTLY`: Set to "1" to build and run the *entire* test suite against torch nightly (full run, not just the tagged subset). Also forces a full run on the pinned torch. Intended to be set on the scheduled build.
*   `RUN_ALL`: Set to "1" to force run all steps.
*   `SKIP_TIMEOUT`: Set to "1" at pipeline generation time to omit all configured step timeouts.
*   `VLLM_CI_DISABLE_HF_OFFLINE_RETRY`: Strict `0`/`1` emergency switch read during pipeline generation and AMD job startup.
*   `DOCS_ONLY_DISABLE`: Set to "0" to enable skipping CI for doc-only changes.
*   `VLLM_USE_PRECOMPILED`: Set to "1" to force use of precompiled wheels.
*   `VLLM_CI_ONLY_STEP_KEYS`: A non-empty JSON array of stable step keys. When
    set, the generator emits those steps and their transitive dependencies,
    ignoring normal source-file selection. Unknown keys fail generation.

Kubernetes-backed test jobs read `BUILDKITE_ANALYTICS_TOKEN` from the
`buildkite-analytics-token-secret` Secret's `token` key when it is available.
The Secret is optional, so missing credentials do not prevent jobs from running.
Provision it in a Buildkite job namespace with
`infra-k8s/create-buildkite-analytics-secret.sh` to enable test result uploads.
