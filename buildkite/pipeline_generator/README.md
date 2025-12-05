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
```

## Environment Variables

The generator relies on several environment variables, typically provided by Buildkite or set by user:

*   `BUILDKITE_BRANCH`: Current branch name.
*   `BUILDKITE_COMMIT`: Current commit hash.
*   `BUILDKITE_PULL_REQUEST`: Pull request number (or "false").
*   `BUILDKITE_PULL_REQUEST_BASE_BRANCH`: Base branch for PRs.
*   `NIGHTLY`: Set to "1" to force run nightly steps.
*   `RUN_ALL`: Set to "1" to force run all steps.
*   `DOCS_ONLY_DISABLE`: Set to "0" to enable skipping CI for doc-only changes.
*   `VLLM_USE_PRECOMPILED`: Set to "1" to force use of precompiled wheels.
