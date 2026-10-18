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

# Optional. Directories of queue fallback entries; absent turns the feature off.
# See "Queue fallback" below.
fallback_dirs:
  - ".buildkite/queue_fallbacks"
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

## Queue fallback

CI stops sending new work to queues that are known to be unhealthy. A queue,
or a group of queues on one cluster, whose recent infra failure rate in `main`
builds crosses its threshold is closed for a while, and new builds run its
steps on the next device of a static, declared chain. Shadow probes in later
`main` builds decide when it reopens. Nothing ranks or picks queues: health
only decides whether to skip to the next declared entry.

Only CUDA and AMD GPU devices take part:

- CUDA: `h100`, `h200`, `h200_18gb`, `h200_35gb`, `b200`, `b200-k8s`, `l4`
- AMD: the keys of `amd.AMD_DEVICE_CONFIGS` (`mi250_*`, `mi300_*`, `mi355_*`,
  `mi355_dpx`)

Every other device (`a100`, `cpu*`, `intel_*`, `arm_cpu`, `gh200`,
`ascend_npu`, `amd_cpu`, `zen5`, `dgx-spark`) and every step without a device is never
mapped and never probed; its output is unchanged.

```
every 10 min   queue-health pipeline (.buildkite/pipelines/queue-health.yml)
               ├─ read the policies and pins under the fallback_dirs of vLLM
               │    main's .buildkite/ci_config*.yaml (sparse checkout)
               ├─ read the snapshot (stale or not) and the recent jobs of the
               │    scanned pipelines' main builds (REST, read_builds)
               ├─ for each watched queue or group: apply pins, rules and probe
               │    results -> open / closed / probing
               ├─ last: publish the snapshot as build meta-data "queue-health"
               └─ any error: publish nothing

every build    pipeline generator (queue_fallbacks.py)
               ├─ no fallback_dirs: exactly as before
               ├─ read the chains, then the snapshot once (latest passed
               │    queue-health build on main)
               │    missing, older than max_age or malformed: every queue is open
               ├─ each step: first device in its chain whose queue is open
               │    (all closed: its own device)
               ├─ render the step for that device with the usual converters
               ├─ main builds only: add the shadow probes of probing queues
               └─ annotate the build (context queue-fallback)
```

### Ownership

Every fact has one owner and is read, never derived again:

| Fact | Owned by | Read by |
|---|---|---|
| `fallback_dirs` of a pipeline config | pipeline config | generator (global config) and checker, both through `config_fallback_dirs` |
| Chains (`device` + `fallbacks`) | config | generator (`load_chains`) |
| Policies (`defaults`, policy keys of `device` and `group` entries), groups and pins | config | checker (`load_health_policies`) |
| The `health` entry | config | both |
| Supported devices, device queues, platforms, probe keys | `queue_fallbacks.py` | both, imported |
| Which jobs are infra failures | `fallback_rules/common.py` | checker, imported |
| Queue state, pins included | checker, one publish per run | generator and the next checker run (`read_snapshot`) |

The snapshot is the only source of queue state. The generator has no state
logic: it applies no pins and evaluates no rules, it only skips queues whose
snapshot entry is not `open`. A pin therefore takes effect at the first
checker run after it lands on vLLM `main` (within about 10 minutes), and so
does its expiry. A snapshot older than `max_age` means every queue is open,
pinned ones too.

Both loaders share one parser, so every entry is checked the same way, but
each component keeps only its own entries. Everything they return is frozen
(frozen models and dataclasses, read-only mappings, tuples), and so is the
snapshot: the generator reads it once and passes the same object to step
mapping and probe rendering; the checker builds the next one in a single pass
and publishes it unchanged.

### Configuration

`fallback_dirs` in the pipeline config lists directories in the vLLM repo;
anything but a list of non-empty paths is an error, and a repeated path is
read once. One parser, `config_fallback_dirs`, reads it for the generator and
the checker. Each directory holds `common/`, `nvidia/` and `amd/` subdirectories of `*.yaml`
files, read in sorted order; a missing subdirectory is empty. Every file is a
list of small, independent entries, each exactly one of `defaults`, `device`,
`group`, `pin` or `health`. Durations are written as `30m`, `2h` or `1d`.

Mistakes in any entry fail generation with the file and entry number: an
unknown key or bad value, an unknown device, a second entry for a device, a
device in its own fallbacks, a second pin for a queue, a pin `until` without
a timezone, or a missing or second `health` entry; for groups, a `group` in
`common/`, a second group of the same name, a device listed twice or in two
groups, a device of another platform than the group's directory, and policy
keys on the `device` entry of a grouped device (`<d> is in group <g>: set its
policy on the group`). A device outside the supported set fails with
`unsupported device for queue fallback: <d> (only CUDA and AMD GPU devices)`.
Mistakes that only show once policies are merged fail the checker, not
generation: an unknown rule or rule parameter, a `device` or `group` entry
with policy keys whose merged policy is still incomplete, a pin on a device
that is not watched, or a second pin in one group.

```yaml
# common/health.yaml: exactly one, in common/
- health:
    pipeline: queue-health            # slug of the checker pipeline
    token_secret: QUEUE_FALLBACK_BUILDKITE_API_TOKEN
    max_age: 30m                      # an older snapshot is ignored
    pipelines: [ci]                   # pipelines whose jobs are scanned and probed

# amd/defaults.yaml: the AMD policy; no common defaults, so NVIDIA is not watched
- defaults:
    close_when: {rule: infra_failure_rate, above: 0.67, window: 30m, min_jobs: 20}
    reopen_after: 2h                  # first backoff, and each probe round's length
    probes: 3                         # shadow steps per probe round

# amd/clusters.yaml: queues on one cluster share one state
- group:
    name: mi300                       # slug, unique
    devices: [mi300_1, mi300_2, mi300_4, mi300_8]   # the first runs the probes
- group:
    name: mi355
    devices: [mi355_1, mi355_2, mi355_8]

# amd/mi300.yaml: chains
- device: mi300_1
  fallbacks: [mi355_dpx, mi355_1]
- device: mi300_2
  fallbacks: [mi355_2]
```

Optional forms, not used in the vLLM config today:

```yaml
# a group or an ungrouped device entry can override any policy key
- device: mi355_dpx
  fallbacks: [mi355_1, mi300_1]
  close_when: {rule: infra_failure_rate, above: 0.5, window: 1h, min_jobs: 10}

# reviewed, time-boxed manual pin
- pin: {device: mi355_dpx, state: closed, until: "2026-11-01T00:00:00Z", reason: "node upgrade"}
```

A device's queue is the one the generator already sends it to. The checker
watches a device only if its merged policy is complete (`close_when`,
`reopen_after` and `probes`): the common defaults, then its platform's
defaults, then its `group` entry or, if ungrouped, its own `device` entry;
each top-level key is replaced whole, so an entry's `close_when` replaces the
default one. A device without a complete policy is not watched: it has no
snapshot entry, is never closed or probed, and stays open in every chain. So
policies can be set for one platform only, as above. A `device` or `group`
entry that sets policy keys must complete its policy. A watched queue that is
in no chain can close and be probed, but no step moves off it.

A `group` (in `amd/` or `nvidia/`, all its devices of that platform, a device
in at most one group) is the unit of health for queues on one cluster: the
rule sees the pooled jobs of all its queues (`window` and `min_jobs` over the
pool), there is one state, and every member queue's snapshot entry mirrors it,
its reason prefixed `group <name>: `. A grouped device's policy is set on the
group only; its `device` entry may hold a chain and nothing else. An
ungrouped device is a unit of one. A lone queue follows the policy of the
first supported device on it by name; today every supported queue has
exactly one device.

Pins are kept by unit. While a pin's `until` is in the future, the checker
writes the pin's `state` and `until` into the snapshot with the reason
`pinned: <reason>`, for every queue of its group. `state: closed` takes the
queues out of every chain; `state: open` keeps them open whatever the rules
say. A pin needs a watched device, and a group takes one pin. Once the pin
expires or is removed, the queues go back to the rules as `open`, and
failures from before that count.

Steps need nothing new. A step follows its device's chain, and its AMD mirror
(`mirror.amd.device`) follows that device's chain. `fallback: false` on a step
keeps it, and its mirror, on their own devices.

### Rules

A rule is a named Python predicate in `fallback_rules/`: it gets a list of
jobs on a queue plus the parameters from `close_when` (every key except
`rule`, `window` and `min_jobs`), and returns True when the queue should be
closed. `common.py` holds rules for every platform; `nvidia.py` and `amd.py`
can add rules or override common ones by name.

```python
# fallback_rules/common.py
RULES = {
    "infra_failure_rate": infra_failure_rate,  # (jobs, above) -> bool
}
```

`infra_failure_rate` counts a job as an infra failure only when it `failed`
with one of these:

| Exit status | Signal reason |
|---|---|
| -1 | `stack_error`, `process_run_error`, `agent_stop`, `agent_refused`, `agent_incompatible`, or none (agent lost) |
| 255 | `stack_error`, `process_run_error`, `agent_stop`, `agent_refused`, `agent_incompatible` |

Exit 255 without a signal reason is a test exiting 255, not infra. Timeouts
and test failures do not count, so a PR with a real bug cannot close a
healthy queue.

### Health checker

`queue_health.py` runs in the `queue-health` pipeline on a CPU queue from a
depth-1 sparse checkout of vLLM main that holds only the pipeline configs and
the directories they list in `fallback_dirs`:

```bash
python3 queue_health.py .buildkite/ci_config*.yaml
```

It reads the union of the configs' `fallback_dirs` (each parsed by
`config_fallback_dirs`), in order and without duplicates, relative to the
working directory (the repo root). If no config sets it, the checker prints an error and exits 1 before reading the secret,
publishing nothing.

As the checker loads every listed directory as one config, all `ci_config*`
files that set `fallback_dirs` must share the same `common/` (one `health`
entry for all of them), and no device, pin queue or platform `defaults` may
be declared in two of their directories.

A run reads the token, the current snapshot, then the jobs of the
`health.pipelines` builds on branch `main` that finished within the longest
`close_when.window` or since the oldest probing round began, or are still
running. PR builds never count, so a broken PR cannot close a queue. Routing
still applies to every build. It computes every watched queue's next entry,
prints them as a table, and as its last action
publishes the new snapshot. Any error before that (config, token, the
previous snapshot, job history, a bug) fails the build and publishes
nothing. Concurrency 1 makes the checker the snapshot's only writer.

**Outage.** When the checker fails, for example because the Buildkite API
does, nothing is published. Builds keep using the last snapshot until it is
older than `max_age` (30m above), then treat every queue as open and run
exactly as without queue fallback, so an outage holds the last known states
for at most `max_age`. A build that cannot read the snapshot itself treats
every queue as open at once.

There is one snapshot definition, `read_snapshot`: the meta-data
`queue-health` of the latest passed build of `health.pipeline` on branch
`main`, so a build of the checker from another branch is never read. The
generator and the checker both call it; only the checker accepts a snapshot
older than `max_age`, to carry its state over. Without any passed `main`
build every queue starts open.

| State | Meaning | Next |
|---|---|---|
| `open` | Takes new steps | `closed` for `reopen_after` (longer when flapping) when the rule fires on at least `min_jobs` jobs |
| `closed` | Skipped by its chains | `probing` at the first run at or after `until` |
| `probing` | Still skipped; `main` builds run its probes | At `until`: `closed` for twice the last backoff or `open` (see Probes) |

An open queue's rule sees the jobs that finished (`passed`, `failed`,
`timed_out` or `expired`) within `close_when.window`, on all of its group's
queues. While a queue is closed or probing before `until`, only its
`infra_rate` and `jobs` are refreshed. Anti-flap, backoff and probe rounds
work the same for a group as for a lone queue.

**Anti-flap.** A queue reopened by a probe round counts only jobs that
became runnable after that, so the failures that closed it, even those that finish later,
cannot close it again. A queue
that closes again within `2 x reopen_after` of reopening doubles its backoff
instead of starting over. A doubled backoff is capped at 8h, or at
`reopen_after` when that is longer, so doubling never shrinks it. `probe_round` and `backoff_s` carry
over across closes, reopens and pins: the next round is always the previous
one plus 1, so a probe key never repeats.

### Probes

AMD test steps download their image with `--step image-build-amd`, an
artifact of the same build, so a probe cannot run in another build. Probes
are therefore shadow steps inside the next `main` builds of the scanned
pipelines, not in the `queue-health` build.

When a closed queue is due, the checker starts a probe round. The round
starts then (`since`) and ends at a fixed deadline, `until = since +
reopen_after`:

```json
"amd_mi300_1": {"state": "probing", "since": "2026-10-16T13:40:00Z",
  "until": "2026-10-16T15:40:00Z", "probe_round": 1, "backoff_s": 7200,
  "probe": {"round": 1, "device": "mi300_1", "keys": ["amd-kernels", "amd-basic-correctness"]}}
```

- `device` is the device whose policy the queue follows: a group's first
  device, and only that device's queue carries the `probe`, so each probe
  runs once for the whole group.
- `keys` are up to `probes` distinct step keys of the jobs that finished on
  that queue (only that queue, for a group) within `close_when.window`,
  infra failures first, then newest
  first, chosen again for every round. A probe job counts as the step it
  probed, and an AMD mirror is keyed `amd-<key>`, as in the build. When that
  window has no jobs, as after a backoff longer than it, the round uses the
  keys a closed queue records as its `probe` when it closes: its next round,
  on the keys of the jobs that closed it.

When the global config's branch is `main` (never in PR builds, nor in
builds limited to selected step keys or recording builds), the generator
adds, for each key present in the build, one copy of that step pinned to
`device`:

- rendered by the usual converters exactly like a normal step on that device
  (an AMD mirror through `mirror.amd.device`), from the step as written, so it
  is never itself moved by a chain;
- one job: no automatic sharding, `parallelism`, `concurrency` or
  `concurrency_group`, on the step or its AMD mirror;
- keyed `probe-<queue>-r<round>-<key>`, with characters outside
  `[A-Za-z0-9_-]` replaced by `-` and cut to 100 characters, e.g.
  `probe-amd_mi300_1-r1-amd-kernels`;
- `soft_fail: true`, labelled `Probe <queue> r<round>: ...`, in a
  `Queue probes` group, and nothing depends on it;
- listed in the build's annotation.

A key that is not in the build, a step whose platform differs from the probe
device's, or a probe device that is not on the queue gets no probe.

At the first checker run at or after the deadline, each key of the round
gives at most one piece of evidence: of the jobs on the queue keyed
`probe-<queue>-r<round>-<key>`, in the order they became runnable, the first
one that gives evidence, judged as it was at the deadline:

| Probe job at the deadline | Evidence |
|---|---|
| None, or not yet runnable | None |
| Runnable but never started | An infra failure (`failed`, exit -1) |
| Started but not finished | None |
| Finished `passed`, `failed`, `timed_out` or `expired` | The job as it ended, classified by the rule |
| Finished `canceled`, `skipped` or `broken` | None |

A probe that could run but never started counts as an infra failure because
on a broken queue that is often all there is to see before the deadline: on
a Kubernetes agent stack, a job whose pod cannot be scheduled never starts,
and REST reports it `failed` only hours after it became runnable, long after
the round ended.

The queue stays closed, for twice the last backoff, only if there is at least
one piece of evidence and the queue's own `close_when` rule fires on it
(`min_jobs` does not apply). Otherwise it reopens, and the reason gives the
evidence, e.g. `probe round 2: 0 infra failures in 3 probes; reopened`.
Missing `main` builds and probed steps that left `main` give no evidence, so
they cannot keep a queue closed.

The result is published as the build's meta-data `queue-health`:

```json
{"version": 1, "generated_at": "2026-10-16T12:00:00Z",
 "queues": {"amd_mi300_1": {"state": "closed", "since": "2026-10-16T11:40:00Z",
   "until": "2026-10-16T13:40:00Z", "reason": "group mi300: infra_failure_rate: 72% infra failures in 31 jobs over 30m",
   "infra_rate": 0.72, "jobs": 31, "probe_round": 0, "backoff_s": 7200,
   "pinned": false, "probe": {"round": 1, "device": "mi300_1", "keys": ["amd-kernels"]}},
  "amd_mi300_2": {"state": "closed", "since": "2026-10-16T11:40:00Z",
   "until": "2026-10-16T13:40:00Z", "reason": "group mi300: infra_failure_rate: 72% infra failures in 31 jobs over 30m",
   "infra_rate": 0.72, "jobs": 31, "probe_round": 0, "backoff_s": 7200, "pinned": false}}}
```

Every watched queue has an entry, and only those. A snapshot with any unknown or malformed
field is invalid as a whole, so the generator treats every queue as open.

### Pipeline settings

The `queue-health` pipeline uploads `.buildkite/pipelines/queue-health.yml`.
No Terraform resource manages it, as for `rebuild-cpu-ami`. In its Buildkite
settings:

- Same cluster as the GPU queues and the secret it reads.
- A schedule every 10 minutes on branch `main` (only `main` builds are read).
- "Cancel intermediate builds" off: each run starts from the previous
  snapshot.
- The step already sets concurrency 1 (`ci-infra/queue-health`) and a
  10-minute timeout.

### Secrets

The token is read at run time with `buildkite-agent secret get <token_secret>`
and never appears in generated YAML or logs.

`QUEUE_FALLBACK_BUILDKITE_API_TOKEN` is a dedicated Buildkite cluster secret
holding a read-only API token with the `read_builds` scope only; the checker
and the generator use no other API. Its access policy lets the
`queue-health` pipeline and the bootstrap steps of `ci` and `amd-ci` read it.
A Buildkite admin has to create it. Until then, `token_secret:
CRCR_BUILDKITE_API_TOKEN` (`read_builds`) can be used as an interim. Future
rules on live queue data (agents, waiting jobs) may need `read_clusters` and
GraphQL access as well.

### Safety

| Situation | Behavior |
|---|---|
| No `fallback_dirs`, no chain for a device, or `fallback: false` | As before |
| A device without a complete policy | Never closed or probed; always open in chains |
| PR builds | Routed by the snapshot, but their jobs never count toward health |
| A device other than the supported CUDA and AMD GPU devices | Never mapped or probed; naming it in config fails generation |
| Snapshot missing, stale, malformed, or the API unreachable | Every queue open, pins ignored, with a one-line note in the log |
| Every queue in a chain closed | The step's own device, with an annotation |
| Fewer than `min_jobs` recent jobs (pooled for a group) | The queue stays open |
| Probes | Only in `main` builds, `soft_fail`, nothing depends on them, never in PR builds |
| A checker error or Buildkite API outage | Nothing published; after `max_age` the snapshot is stale and every queue is open |
| A checker bug that publishes | Can only change which declared device a step uses, and add soft-failing probes to `main` builds, and only in builds generated after it |

### Rollback

| Scope | How |
|---|---|
| Whole pipeline | Remove `fallback_dirs` |
| One device | Delete its chain entry |
| One step | `fallback: false` |
| One queue now | A `pin` entry (next checker run), or let the probes reopen it |
| The checker | Pause its schedule; the snapshot goes stale, every queue is open and no probes run |

### Limits

- Routing is decided once, when the pipeline is generated. A job already
  running or waiting when its queue closes is not moved; it gets the usual
  same-queue automatic retry.
- Only declared chains are used, and only infra failures close a queue.
- While a queue is probing, each `main` build runs up to `probes` extra GPU
  jobs on it. A round without evidence reopens the queue untested.
- A probe that waits for an agent through the whole round counts as an infra
  failure, even if the queue is only busy.
- A group probes only on its first device. If it closed on failures of other
  members and the first device had no jobs in the window and no keys recorded
  at close, the round has no probes and the group reopens at the deadline.
- While the checker cannot publish (history or snapshot unavailable), no
  queue changes state; past `max_age` every queue is open and no probes run.
- A latest passed snapshot that no longer parses (for example after a schema
  change) fails every checker run; meanwhile every queue is open.

## Environment Variables

The generator relies on several environment variables, typically provided by Buildkite or set by user:

* `BUILDKITE_BRANCH`: Current branch name.
* `BUILDKITE_COMMIT`: Current commit hash.
* `BUILDKITE_PULL_REQUEST`: Pull request number (or "false").
* `BUILDKITE_PULL_REQUEST_BASE_BRANCH`: Base branch for PRs.
* `NIGHTLY`: Set to "1" to auto-run the curated torch-nightly steps (those tagged `mirror.torch_nightly`).
* `TORCH_NIGHTLY`: Set to "1" to build and run the *entire* test suite against torch nightly (full run, not just the tagged subset). Also forces a full run on the pinned torch. Intended to be set on the scheduled build. AMD build/test steps and AMD mirrors are dropped entirely for this run, since ROCm uses its own pinned torch and gains no signal from validating against a CUDA torch-nightly build.
* `RUN_ALL`: Set to "1" to force run all steps.
* `SKIP_TIMEOUT`: Set to "1" at pipeline generation time to omit all configured step timeouts.
* `DOCS_ONLY_DISABLE`: Set to "0" to enable skipping CI for doc-only changes.
* `VLLM_USE_PRECOMPILED`: Set to "1" to force use of precompiled wheels.
* `VLLM_CI_KERNREC`: Set to "1" to load the kernel-launch recorder (`buildkite/ci_selector/recorders/kernrec`) into every CUDA process of every GPU step and upload its `.kernrec/**` output as job artifacts. Off by default; meant for nightly and post-merge builds that feed the test selector.
* `VLLM_CI_FNREC`: Set to "1" to load the Python function recorder (`buildkite/ci_selector/recorders/fnrec`) into every Python process of every step, writing to its own `.fnrec/<job-id>`. Off by default; independent of `VLLM_CI_KERNREC`, same audience.
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
