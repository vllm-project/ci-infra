# Nightly Tests Failure Notification Fix

## Problem

The "Nightly Tests Failure Notification" step was not working properly during nightly builds because:

1. The original wait step used `wait: ~` which waits for **ALL** previous pipeline steps
2. Many tests had manual block steps that required human approval:
   - `block-build-cu118` - CUDA 11.8 image build
   - `block-arm64-cpu-img-build` - ARM64 CPU image build
   - `block-intel-cpu` - Intel CPU test
   - `block-ibm-power` - IBM Power test
   - `block-ibm-ppc64-test` - IBM Power ppc64le test
   - `block-ibm-s390x` - IBM Z test

3. During nightly runs with `NIGHTLY="1"` and `RUN_ALL="1"`, these blocks were never manually approved, causing the wait to hang forever
4. The notification step never executed because it was waiting for blocked steps to complete

## Solution

### Strategy: Explicit Wait Dependencies with Checkpoints

Instead of using `wait: ~` (waits for everything), we implemented strategic checkpoint wait steps and made the final wait depend only on those checkpoints.

### Changes Made

#### 1. Checkpoint after Main GPU Tests (line ~488-492)

```jinja
{% if nightly == "1" %}
# Checkpoint: wait for all main GPU tests to complete before continuing
- wait:
  key: main-tests-complete
  continue_on_failure: true
{% endif %}
```

This checkpoint is placed after the `{% for step in steps %}` loop completes, ensuring all main GPU tests are done.

#### 2. Checkpoint after AMD Tests (line ~637-642)

```jinja
{% if nightly == "1" %}
# Checkpoint: wait for all AMD tests to complete
- wait:
  key: amd-tests-complete
  continue_on_failure: true
{% endif %}
```

This checkpoint is placed after the AMD test group completes.

#### 3. Key Added to GH200 Test (line ~752)

```jinja
- label: "GH200 Test"
  key: gh200-test
  depends_on: ~
  soft_fail: true
  agents:
    queue: gh200_queue
  command: nvidia-smi && bash .buildkite/scripts/hardware_ci/run-gh200-test.sh
```

Added explicit `key: gh200-test` so we can reference it in the wait dependencies.

#### 4. Final Wait with Explicit Dependencies (line ~773-778)

```jinja
- wait:
  depends_on:
    - main-tests-complete
    - amd-tests-complete
    - gh200-test
  continue_on_failure: true

- label: "Nightly Tests Failure Notification"
  soft_fail: true
  agents:
    queue: small_cpu_queue
  commands: |
    # Gather test results...
```

The notification wait now depends ONLY on:
- Main GPU tests checkpoint
- AMD tests checkpoint
- GH200 test

## How It Works

### During Nightly Runs (`NIGHTLY="1"`):

1. **Main GPU tests execute** → checkpoint wait is created at the end
2. **AMD tests execute** → checkpoint wait is created at the end
3. **GH200 test executes** → has explicit key
4. **Blocked steps exist but are ignored** (CUDA 11.8 build, ARM64 build, Intel/IBM tests)
5. **Final wait** triggers when the three checkpoints complete
6. **Notification executes** and gathers results from all completed tests

### Tests Included in Notification:

The notification checks results from the `steps` variable (main test suite from `.buildkite/test-pipeline.yaml`), which includes:
- All main GPU tests (L4, A100, H100, H200, B200)
- Multi-GPU distributed tests
- Various vLLM feature tests

### Tests Excluded from Wait (Won't Block Notification):

- CUDA 11.8 image build (blocked)
- ARM64 CPU image build (blocked)
- Torch nightly tests (in separate group with `depends_on: ~`)
- Intel CPU/HPU/GPU tests
- IBM Power tests
- IBM Z tests
- Neuron tests
- Ascend NPU tests

These tests can exist and run (if manually approved or have agents), but they won't prevent the notification from running.

## Benefits

1. ✅ Nightly notification runs as soon as core tests complete
2. ✅ No hanging on manual approval blocks
3. ✅ Blocked builds/tests don't interfere with notification
4. ✅ Original test structure preserved (no blocks removed)
5. ✅ Explicit control over which test groups to wait for

## Technical Notes

### Why Not `wait: ~`?

Buildkite's `wait` step has only two modes:
- `wait: ~` - waits for ALL previous steps (including blocks)
- `wait` with `depends_on: [keys]` - waits ONLY for specified keys

There's no "exclude" or "skip" pattern available, so we must use explicit dependencies.

### Why Checkpoints?

Individual test steps in the `{% for step in steps %}` loop don't have explicit keys by default. Rather than adding keys to hundreds of tests, we place a single checkpoint wait after the loop completes. This checkpoint implicitly waits for all tests in that section.

### Nightly Environment Variables

```bash
AMD_MIRROR_HW="amdexperimental"
NIGHTLY="1"
RUN_ALL="1"
```

These variables ensure:
- All optional tests run (no `ns.blocked == 1` conditions)
- Checkpoints are created (only when `nightly == "1"`)
- AMD tests execute on experimental hardware

## Testing

To test these changes:
1. Create a build with environment variable: `VLLM_CI_BRANCH=<your-branch>`
2. Set `NIGHTLY="1"` and `RUN_ALL="1"`
3. Verify the notification step runs after main/AMD/GH200 tests complete
4. Verify it doesn't wait for blocked Intel/IBM tests

## Files Modified

- `buildkite/test-template-ci.j2` - Jinja2 template for CI pipeline
