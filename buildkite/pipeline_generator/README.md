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

`pipeline-generator explain` and `pipeline-generator lint` are tools for the
`job_dirs` yaml (see "Steps that name their devices" below).

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

# List of regex patterns to exclude from run_all_patterns checks.
# If a file matches run_all_patterns but ALSO matches one of these,
# it will NOT trigger a "run all".
run_all_exclude_patterns:
  - "docker/Dockerfile."
  - "csrc/cpu/"

# Optional. run_all_patterns matches that only the CUDA build reads: they run
# every CUDA step but not every AMD step.
cuda_only_run_all_patterns:
  - "docker/Dockerfile$"

# Optional. List of regex patterns to trigger all AMD tests, on top of
# run_all_patterns (which already triggers AMD too). For files that matter to
# AMD but are excluded or left out above, e.g. docker/Dockerfile.rocm_base.
amd_run_all_patterns:
  - "csrc/"
  - "docker/Dockerfile.rocm_base"

# Optional. List of regex patterns to exclude from amd_run_all_patterns checks.
# run_all_exclude_patterns does not apply here.
amd_run_all_exclude_patterns:
  - "csrc/cpu/"

# Container registry to store images
registries: public.ecr.aws/q9t5s3a7

# Repository names for different stages
repositories:
  main: "vllm-ci-postmerge-repo"    # Used for main branch builds
  premerge: "vllm-ci-test-repo"     # Used for PR/pre-merge builds
```

## Steps that name their devices (`devices:`)

A step in a `job_dirs` file names its hardware only as the keys of
`devices:`. Each key is one job on that device; more jobs on the same device
are that device's `variants:`. A step without `devices:` (the older `device`
plus `mirror.amd` form) loads as before, so a file can mix both while it is
migrated.

```yaml
- label: V1 Attention                 # the bare title
  key: v1-attention
  devices:
    h100:
      timeout_in_minutes: 85
      commands:
        - pytest -v -s v1/attention
      variants:
        eager:                         # another H100 job: the H100 one, plus this
          env:
            VLLM_ENFORCE_EAGER: "1"
    b200-k8s:
      same_as: h100                    # the H100 entry, but for what it writes
      timeout_in_minutes: 30
    mi300_1:
      timeout_in_minutes: 125
      source_file_dependencies:
        - vllm/platforms/rocm.py       # added to the shared list
      commands:
        - pytest -v -s v1/attention --ignore=v1/attention/test_mla_backends.py
  automatic_shard: true
  source_file_dependencies:            # every job's
    - vllm/v1/attention/
    - tests/v1/attention
```

This is four jobs: `v1-attention` (`:nvidia: (H100) V1 Attention`),
`v1-attention-eager` (`:nvidia: (H100) V1 Attention [eager]`),
`v1-attention-b200` and `amd-v1-attention` (`:amd: (MI300) V1 Attention`).

**Fields.** Each has one place:

* step only: `label`, `key`, `depends_on` (other steps; never an image step),
  `if`, `autorun_on_main`, `allow_dependency_failure`;
* software, at the step level (every job's) or on a device or variant (that
  job's): `commands`, `source_file_dependencies`, `env`, `working_dir`,
  `automatic_shard`, `mount_buildkite_agent`, `no_plugin`;
* machine, only on a device or variant: `timeout_in_minutes`, `optional`,
  `soft_fail`, `parallelism`, `num_devices` (NVIDIA only: an AMD pool has its
  own count), `num_nodes`, `agent_tags`, `retry`, `concurrency`,
  `concurrency_group`, `no_gpu`, `dind` (only `true`);
* on a device or variant: `key` (the full key, verbatim) and `label` (the
  title only); on a device: `same_as` and `variants`.

**Resolving a job.** Nothing is ever shadowed:

* step and device: a device adds `source_file_dependencies` entries and `env`
  vars (repeating one of the step's is an error); every other software field
  is set at the step level or on the device, never both;
* `same_as: <device>` starts from an earlier device of the same vendor that
  has no `same_as` itself, minus its key, label and variants. What it writes
  replaces the copied value, except that dependencies add and `env` merges
  var by var. Variants are never copied;
* a variant starts from its device's resolved job, minus key and label.
  Dependencies add; anything else it writes replaces, including a step-level
  `commands` (the only override there is, and it is written under the device
  it runs on). It must change something;
* the device registry adds the rest, and a step never writes it: the image
  step (AMD `image-build-amd`, CPU `image-build-cpu`, NVIDIA the file's
  `depends_on`, `dgx-spark` `arm64-image-build`; a CPU job in a file whose
  `depends_on` is `[]` gets none), the step's own `depends_on` after it, and
  for AMD no dind, the ROCm wrapper and the pool's GPU count.

**Labels** are `<icon> (<TAG>) [Nightly ]<title>`, the icon and tag from the
device (`:nvidia: (H200 MIG 35GB)`, `:amd: (MI355 DPX)`, `:computer: (CPU)`),
`[Nightly]` when the job is `optional: true`. A variant's title is its
device's plus ` [<name>]`, unless it sets `label:`.

**Keys**, with K the step's `key`, in map order: the first non-AMD device is
K and any other `K-<key_tag>`; the first AMD device is `amd-K` and any other
`amd-K-<key_tag>`; a variant is `<its device job's key>-<name>`. A `key:` is
used verbatim, and a pinned device's variants derive from it. Two devices of
one key tag (`mi355_1` and `mi355_4`) need a pin on one of them, and every
key must be unique across the pipeline.

**Errors** (when the yaml is loaded):

| Code | What |
|---|---|
| E1 | a device not in the registry (the message lists them), a variant name not `[a-z0-9-]+` |
| E2 | a removed field (`device`, `gpu`, `mirror`, `vendors`, `target`, `run_on`, `num_gpus`, `dind: false`, an image step in `depends_on`, a label starting with an icon, `(HW)` or `[Nightly]`) or an unknown one; the message says where the concept lives |
| E3 | a device shadowing the step (a repeated dependency or env var, a field set at both levels) |
| E4 | a machine field at the step level |
| E5 | a bad `same_as`: another vendor, a later device, a chain, a variant |
| E6 | an empty variant, or a no-op override (`same_as` or variant) |
| E7 | `num_devices` on an AMD (or CPU) device |
| E8 | a duplicate key, or two devices of one key tag without a pin |
| E9 | a flow-style list or map (`[a, b]`, `{a: b}`) in a `devices:` step |

**How it compiles.** The rest of the generator, and ci_selector, read the
older shape, so a `devices:` step is compiled when the yaml is loaded
(`step_vendors.py`): the non-AMD device job keyed K (else the first non-AMD
device's) is the step, the AMD job keyed `amd-` plus that step's key is its
`mirror.amd`, marked `_exact` (the whole job: nothing is taken from the
step), and every other job (more devices, variants, AMD-only tests, pinned
keys that break the pair) is a step of its own. ci_selector reads the yaml
through the same code.

**Adding hardware** is one registry entry; a step can then name the device.
An NVIDIA or CPU device needs its `DeviceType`, its `AgentQueue`, a branch in
`buildkite_step.get_agent_queue()` and an entry in
`constants.NON_AMD_DEVICES` (vendor, label tag, key tag, queue; `image` if it
runs in another image). An AMD device needs its `DeviceType`, its
`AgentQueue` and an entry in `amd.AMD_DEVICE_CONFIGS` (queue, GPU count, arch,
label tag, key tag). For example, a Radeon R9700 pool would be
`DeviceType.AMD_R9700_1 = "r9700_1"`, `AgentQueue.AMD_R9700_1 =
"amd_r9700_1"` (the queue its agents serve) and
`_pool(AgentQueue.AMD_R9700_1, 1, "gfx1201", "r9700")`, after which
`r9700_1:` (plus `same_as: mi355_1`) adds it to a test.

### `pipeline-generator explain` and `lint`

```bash
pipeline-generator explain .buildkite/test_areas/attention.yaml
pipeline-generator explain amd-v1-attention [--job-dir .buildkite/test_areas] [--format json]
pipeline-generator lint .buildkite/test_areas [--fix]
```

`explain` reads a job file (or the file holding a key) as the generator does,
in either form, and prints one row per job: key, device, label, timeout,
optional, commands, dependencies and depends_on (`--format json` adds every
resolved field, to compare two versions of a file job by job). An AMD job's
label, timeout and depends_on are shown as rendered (3-hour default timeout).

`lint` reports the errors above and these, and `--fix` rewrites the yaml text
in place, moving lines with the comments above them:

* L1: with two or more devices, a software value every device has (a
  dependency, an env var, the same commands, `working_dir`, ...) belongs at
  the step level;
* L2: a device written the same as an earlier device of its vendor is
  `same_as: <that device>`;
* L3 (a warning, never fixed): a dependency covered by a broader one at the
  same or a shared scope (`vllm/models/x/` under a step-level `vllm/`);
* L4: the order: label, key, devices, software fields, the other step
  fields; devices NVIDIA, AMD, then CPU (each vendor in its written order); a
  device's same_as, key, label, machine fields, software fields, variants.
  Where the new order would change a default key (a CPU device moved after an
  NVIDIA one), the fix pins the old key.

A fix never changes a job: each fixed step is compiled before and after, and
one whose jobs would differ is left alone and reported. `lint` exits 1 on an
error or an unfixed L1, L2 or L4 finding.

Follow-ups, not part of this change: a CI job that posts the `explain` table
on PRs touching `test_areas`, a check that fails when a key compiled on main
disappears while its step remains (a reordered device or a renamed step
needs `key:` pinned), and `explain --diff-triggers`.

## Jobs that are not selected

A step runs when its `source_file_dependencies` match the change (or on
nightly / `RUN_ALL` builds); optional steps never run on their own, except
that a change to the ROCm stack image (`docker/Dockerfile.rocm_base` or
`tools/vllm-rocm/therock_wheels.py`) runs every AMD step. An AMD step counts
an edit to its own yaml only if the edit changes the step's entry, or can't be
tied to single steps. An optional AMD step whose entry changes runs too, as
ci-selector does for the steps it picks. Every
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
* `AITER_NIGHTLY`: Set to "1" to test vLLM against the latest [AITER](https://github.com/ROCm/aiter) nightly wheel (or the one `AITER_SPEC` pins). Meant for a scheduled build of `main`.
    * Runs only the AMD build steps and the MI300, MI325 and MI355 jobs (gfx942, gfx950). Everything else, CUDA and MI250 included, is dropped; PR builds still run pre-commit.
    * Ignores `NOAUTO` and the docs-only skip.
    * Pushes the image to `rocm/vllm-dev`, never `rocm/vllm-ci`.
    * Turns off the selector recorders (`VLLM_CI_KERNREC`, `VLLM_CI_KERNREC_AMD`, `VLLM_CI_FNREC`).
    * Generation fails with `TORCH_NIGHTLY` or `VLLM_CI_PLATFORM=cuda`, on a vLLM checkout without `.buildkite/scripts/rocm/build.sh`, or if a kept step depends on a dropped one.
* `RUN_ALL`: Set to "1" to force run all steps.
* `VLLM_CI_IMAGES_STEP`: The key of the step that runs `pipeline-generator --images_only` beside bootstrap. Set on bootstrap, so it leaves the early images to that step (see "Image builds start before the test selector").
* `VLLM_CI_EARLY_IMAGE_UPLOAD`: Set to "0" to upload the whole pipeline at once, early images included.
* `SKIP_TIMEOUT`: Set to "1" at pipeline generation time to omit all configured step timeouts.
* `DOCS_ONLY_DISABLE`: Set to "0" to enable skipping CI for doc-only changes.
* `VLLM_USE_PRECOMPILED`: Set to "1" to force use of precompiled wheels.
* `VLLM_CI_KERNREC`: Set to "1" to load the kernel-launch recorder (`buildkite/ci_selector/recorders/kernrec`) into every CUDA process of every GPU step and upload its `.kernrec/**` output as job artifacts. Off by default; meant for nightly and post-merge builds that feed the test selector.
* `VLLM_CI_KERNREC_AMD`: Set to "1" to do the same on AMD steps with the ROCm backend. Independent of `VLLM_CI_KERNREC`: either backend can be recorded alone.
* `VLLM_CI_FNREC`: Set to "1" to load the Python function recorder (`buildkite/ci_selector/recorders/fnrec`) into every Python process of every step, writing to its own `.fnrec/<job-id>`. Off by default; independent of `VLLM_CI_KERNREC`, same audience.
* `VLLM_CI_ONLY_STEP_KEYS`: A non-empty JSON array of stable step keys. When
    set, the generator emits those steps and their transitive dependencies,
    ignoring normal source-file selection. Generated AMD mirror keys such as
    `amd-multi-modal-processor` are accepted and select only the mirror plus
    its AMD dependencies. Unknown keys fail generation. With
    `AITER_NIGHTLY=1`, so does a key for a step the AITER nightly does not
    run, such as a CUDA or MI250 job.

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
