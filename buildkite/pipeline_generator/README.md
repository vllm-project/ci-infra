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

## Jobs that are not selected

A step runs when its `source_file_dependencies` match the change (or on
nightly / `RUN_ALL` builds); optional steps never run on their own. Every
other step is left out of the uploaded pipeline. One input step, **Run more
jobs**, near the top of the build lists them all as checkboxes under their
group names (AMD steps under "AMD - <group of their NVIDIA step>"). Buildkite
shows a select of six or more options as a dropdown, `multiple` or not, so
each job is a one-option select of its own, labelled with its group's name
if it is the group's first and with a zero-width space otherwise (Buildkite
rejects a blank label). Tick any and
submit, and a job behind it uploads the ticked steps together with the
not-yet-uploaded steps they depend on. It then adds a new **Run more jobs** input step for the
rest, so more can be picked later.

The left-out steps are written to `<output dir>/deferred/deferred_steps.json`,
next to a copy of `unblock_jobs.py`, and uploaded as artifacts of the
bootstrap job; the upload job runs that copy, so it uploads exactly what was
generated. The input step also has an optional **Job keys** text field
(`more-jobs`; later rounds use `more-jobs-2`, `more-jobs-3`, ...) that takes
step keys one per line, so to pick through the API, unblock the input step
with `{"fields": {"more-jobs": "<key>\n<key>"}}`.

## Image builds start before the test selector

The test selector takes minutes per build, and no test can start before its
image. So the CUDA image (`image-build`), its smoke test, the ROCm image
(`image-build-amd`) and the AMD base steps go up before the selector runs
(`EARLY_IMAGE_KEYS`). Every non-doc build needs them: the CUDA image is the
selector's floor, and the AMD steps keep today's always-run rules. The other
images (CPU, XPU, HPU, arm64, ...) go up with the pipeline, only when a
selected step depends on them.

Buildkite holds the steps a job uploads until that job exits. Uploaded by
bootstrap, the early images still wait out the selector. To start them sooner,
a second step beside bootstrap runs `pipeline-generator --images_only`: it
uploads them and exits within about a minute of getting an agent, then records
the keys it uploaded in the build meta-data `vllm-ci-early-images` (empty when
none: a doc-only change, or a failed upload). Bootstrap, given that step's key
in `VLLM_CI_IMAGES_STEP`, does not upload them; after the selector, it reads
the record and leaves those keys out of its pipeline. If the step finishes
without a record, or has none after 10 minutes, bootstrap uploads the images
itself, as it does without an images step; an images step that comes later
then fails its own upload (Buildkite rejects a key already in the build),
which costs nothing.

In the `vllm/ci` pipeline settings:

```yaml
steps:
  - label: ":docker: images"
    key: bootstrap-images
    agents:
      queue: small_cpu_queue_premerge
    soft_fail: true
    retry:
      automatic:
        - exit_status: -1  # Agent was lost
          limit: 2
    commands:
      - |
        if [[ -f .buildkite/.pipeline_gen_v2 ]]; then
          pip install "git+https://github.com/vllm-project/ci-infra.git@${VLLM_CI_BRANCH:-main}#subdirectory=buildkite/pipeline_generator"
          /var/lib/buildkite-agent/.local/bin/pipeline-generator --pipeline_config_path .buildkite/ci_config.yaml --images_only
        fi
  - label: "bootstrap"
    key: bootstrap
    env:
      VLLM_CI_IMAGES_STEP: bootstrap-images
    # ... the existing bootstrap step, unchanged
```

Without `VLLM_CI_IMAGES_STEP`, bootstrap uploads the early images itself, as
before.

## Environment Variables

The generator relies on several environment variables, typically provided by Buildkite or set by user:

* `BUILDKITE_BRANCH`: Current branch name.
* `BUILDKITE_COMMIT`: Current commit hash.
* `BUILDKITE_PULL_REQUEST`: Pull request number (or "false").
* `BUILDKITE_PULL_REQUEST_BASE_BRANCH`: Base branch for PRs.
* `NIGHTLY`: Set to "1" to auto-run the curated torch-nightly steps (those tagged `mirror.torch_nightly`).
* `TORCH_NIGHTLY`: Set to "1" to build and run the *entire* test suite against torch nightly (full run, not just the tagged subset). Also forces a full run on the pinned torch. Intended to be set on the scheduled build. Implies `VLLM_CI_PLATFORM=cuda`, so AMD build/test steps and AMD mirrors are dropped: ROCm uses its own pinned torch and gains no signal from validating against a CUDA torch-nightly build.
* `VLLM_CI_PLATFORM`: Set to `amd` or `cuda` to generate only that platform's jobs; every other step is dropped from the pipeline (not deferred into "Run more jobs"). `amd` keeps the AMD jobs: `amd_cpu` (the AMD build steps), the AMD GPUs and `amd-` mirrors. `cuda` keeps every other job (so for now it also keeps the few Intel, Arm and Ascend jobs). Steps are still chosen by the usual rules within the platform, so add `RUN_ALL=1` or `NIGHTLY=1` to run all of them. With `amd`, the test selector does not run, since it never picks AMD steps. `VLLM_CI_ONLY_STEP_KEYS` naming a step outside the platform fails generation.
* `RUN_ALL`: Set to "1" to force run all steps.
* `VLLM_CI_IMAGES_STEP`: The key of the step that runs `pipeline-generator --images_only` beside bootstrap. Set on bootstrap, so it leaves the early images to that step (see "Image builds start before the test selector").
* `VLLM_CI_EARLY_IMAGE_UPLOAD`: Set to "0" to upload the whole pipeline at once, early images included.
* `SKIP_TIMEOUT`: Set to "1" at pipeline generation time to omit all configured step timeouts.
* `DOCS_ONLY_DISABLE`: Set to "0" to enable skipping CI for doc-only changes.
* `VLLM_USE_PRECOMPILED`: Set to "1" to force use of precompiled wheels.
* `VLLM_CI_KERNREC`: Set to "1" to load the kernel-launch recorder (`buildkite/ci_selector/recorders/kernrec`) into every CUDA process of every GPU step and upload its `.kernrec/**` output as job artifacts. Off by default; meant for nightly and post-merge builds that feed the test selector.
* `VLLM_CI_KERNREC_AMD`: Set to "1" to do the same on AMD steps with the ROCm backend. Independent of `VLLM_CI_KERNREC`: either backend can be recorded alone.
* `VLLM_CI_FNREC`: Set to "1" to load the Python function recorder (`buildkite/ci_selector/recorders/fnrec`) into every Python process of every step, writing to its own `.fnrec/<job-id>`. Off by default; independent of `VLLM_CI_KERNREC`, same audience.
* `VLLM_CI_INCLUDE_STEP_KEYS`: A non-empty JSON array of stable step keys to
    add to the normal selection, including optional jobs and their dependencies.
    For example, `["language-models-test-mteb"]`. Applies to this build only;
    unknown or off-platform keys warn and discard the include request, keeping
    the normal selection. Cannot be combined with
    `VLLM_CI_ONLY_STEP_KEYS`. `NOAUTO=1` still defers all jobs.
* `VLLM_CI_ONLY_STEP_KEYS`: A non-empty JSON array of stable step keys. When
    set, the generator emits those steps and their transitive dependencies,
    ignoring normal source-file selection. Generated AMD mirror keys such as
    `amd-multi-modal-processor` are accepted and select only the mirror plus
    its AMD dependencies. Unknown keys fail generation.

Kubernetes-backed test jobs read `BUILDKITE_ANALYTICS_TOKEN` from the
`buildkite-analytics-token-secret` Secret's `token` key when it is available.
The Secret is optional, so missing credentials do not prevent jobs from running.
Provision it in a Buildkite job namespace with
`infra-k8s/create-buildkite-analytics-secret.sh` to enable test result uploads.

## OpenTelemetry command and test timing

The generator automatically instruments every standard NVIDIA and CPU job in
trusted `vllm-project/vllm` main-branch builds. It injects the helpers in
`otel_helpers/`, emits one
`ci.command` span for each configured command, and loads a pytest plugin that
emits one `pytest.test` child span per test. Job YAML does not need an opt-in
field.

Command and pytest spans are written to an ephemeral local spool while tests
run. An `EXIT` trap uploads the complete job batch once, preserving the job's
original exit status. The exporter has a three-second total network deadline
and the shell enforces a four-second hard stop, so an unavailable telemetry
receiver cannot add an unbounded delay to every command.

Pull-request containers are deliberately excluded from this fine-grained
instrumentation because exporting spans currently requires access to the
Buildkite agent binary to mint a short-lived OIDC token. Mounting that binary
into a container running untrusted PR code would cross the CI trust boundary.
AMD mirrors are also excluded because their remote runtime does not expose the
agent binary. Native Buildkite agent tracing can still cover job phases for
both cases when it is enabled on those agents.
