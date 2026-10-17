# ROCm kernel collection

The ROCm recorder uses the ROCProfiler SDK to record dispatched kernel names,
including HIP, AITER, Triton, RCCL, and graph replays. The symbol map joins native
kernel names to the sources and headers compiled into the test image. Python
package attribution, including AITER, comes from the separate function recorder.

## Dependencies and setup

Use the ROCm CI image's pinned ROCProfiler SDK **1.3.2** headers and runtime.
`docker/Dockerfile.rocm_base` in vLLM builds that dependency from its pinned
revision. The recorder rejects other SDK versions until they have been validated.
A C++17 compiler is needed to build the small injection library in the image.

`VLLM_CI_KERNREC=1` enables recording on eligible AMD steps. Native
jobs and single-node Docker jobs are supported; multi-node AMD jobs remain
unarmed until remote injection and artifact collection are validated. On AMD,
`ci_setup.sh` builds the recorder against the installed SDK and checks the runtime
version and shared-library dependencies before exporting the injection variables.
Missing dependencies leave the workload running once without recording; the job
sidecar reports collection unavailable. Test jobs do not install or upgrade ROCm.
Provision missing SDK packages in the image build using the matching ROCm release.

After preflight, setup defaults `VLLM_WORKER_MULTIPROC_METHOD=spawn` and
`VLLM_WORKER_SHUTDOWN_TIMEOUT_SECONDS=30`. An explicit non-spawn worker method
leaves collection unavailable and the workload's process behavior unchanged;
an explicit shutdown timeout is preserved. Recorded library scripts must use
an `if __name__ == "__main__":` guard and support spawned workers.

For a local run, from the ci-infra checkout, using an existing workload environment:

```bash
KERNREC="$PWD/buildkite/ci_selector/recorders/kernrec"
VLLM_DIR=/path/to/vllm
bash "$KERNREC/build-rocm.sh" /tmp/libkernrec_rocm.so
KERNREC_DIR=/tmp/kernrec-run \
VLLM_WORKER_MULTIPROC_METHOD=spawn \
VLLM_WORKER_SHUTDOWN_TIMEOUT_SECONDS=30 \
ROCP_TOOL_LIBRARIES=/tmp/libkernrec_rocm.so \
LD_PRELOAD=/opt/rocm/lib/librocprofiler-sdk.so \
  "$VLLM_DIR/.venv/bin/python" -m pytest "$VLLM_DIR/tests/kernels/core/test_layernorm.py"
```

Use a fresh output directory for each run. Preserve any required existing
`LD_PRELOAD` entries. Preload the SDK, and name the recorder in
`ROCP_TOOL_LIBRARIES`. Torch import initializes the SDK before the first GPU
operation; forking after that point cannot record child kernels. Launchers that
call `os.fork()` directly still require separate validation. Allow workers to
finish shutdown: force-killed processes leave incomplete traces. Avoid
simultaneous GPU profilers; profiler-specific tests are excluded from recording.

## Recording contract

Each process that initializes the SDK writes a durable header before kernel work:

```text
# kernrec v1 backend=rocm recorder=rocprofiler-sdk pid=4242 ppid=4200 exe=...
_Z...native_kernel...
triton_kernel_name
# end records=42 unique=2 dropped=0 errors=0 unresolved=0
```

Only dispatched kernels are written. Code-object registration supplies names;
loading a code object alone is not execution evidence. Normalization removes only
a terminal `.kd` descriptor suffix. Names remain mangled so the build map can join
them without losing template specializations.

The SDK recorder uses lossless buffers, reports callback loss counts, and flushes
periodically (`KERNREC_FLUSH_MS`, default 1000). It retains names flushed before an
abrupt exit. An unresolved dispatch ID, recorder error, dropped record, missing
footer, or unsupported fork-after-initialization prevents the trace from
justifying a skipped test. A clean footer describes the **observed processes**;
neither this recorder nor CUPTI independently inventories every expected worker.
Workers must inherit injection settings and artifact storage. Environment-clearing
launchers and remote workers need separate setup and validation.

Job `passed`, shard `complete`, `recording_available`, and `trace_complete` describe
different facts. A successful job with unavailable recording still passed. Both
native backends require healthy trace completion before using silence to drop a
step. Positive observations can still select steps from incomplete recordings.

## Native source maps

ROCm builds retain compile commands, Ninja header dependencies, and automatic
HIPify provenance. On the vLLM side, `VLLM_KERNEL_SYMBOL_MAP=1` enables map
generation and artifact export. The build runs:

```bash
.venv/bin/python tools/ci/kernel_symbol_map.py --backend rocm \
  --build-root build --source-root . --out dist/kernel_symbol_map.rocm.json.gz
```

The map includes generated-source and generated-header provenance back to the
original checkout. ROCm LLVM tools extract AMDGPU entries from the actual objects.
Unreadable objects, missing dependencies, unsupported relocatable-device-code
bundles, and LLVM bitcode make the map incomplete, including when compiler flags
were hidden in response files. A producer unable to validate all objects writes
an empty map with a failure reason; incomplete maps cannot justify drops. AITER
and other external/JIT kernel names are observable; this vLLM native map does not
claim their external source ownership.

The `kernel-symbol-map-rocm` Docker target exports
`kernel_symbol_map.rocm.json.gz`. The ROCm CI bake wrapper uploads that artifact
when the map opt-in is enabled. Collection partitions recordings by backend and
publishes each valid pair independently:

| Backend | Table | Map | Latest pointer |
| --- | --- | --- | --- |
| CUDA | `kernel_table.json.gz` | `kernel_symbol_map.json.gz` | `latest.json` |
| ROCm | `kernel_table.rocm.json.gz` | `kernel_symbol_map.rocm.json.gz` | `latest.rocm.json` |

Fetch with `ci-fetch-kernel-record` and `ci-fetch-kernel-record --backend rocm`.
Both pairs are read from `coverage-data/` by default. Override ROCm paths with
`CI_SELECTOR_ROCM_KERNEL_TABLE` and `CI_SELECTOR_ROCM_KERNEL_SYMBOL_MAP`.
The existing `--kernel-table` and `--kernel-symbol-map` options select a single
explicit pair. An observation or unusable matching row in one pair prevents
another pair from dropping that same step. Tables and maps from different
backends are never joined.

## rocprofv3 diagnostics

Use the SDK's `rocprofv3` command for richer diagnostic traces in a separate run:

```bash
rocprofv3 --kernel-trace --marker-trace --rccl-trace \
  --hip-runtime-trace --mangled-kernels -f json -d /tmp/rocm-json-run -- \
  "$VLLM_DIR/.venv/bin/python" -m pytest "$VLLM_DIR/tests/kernels/core/test_layernorm.py"
```

Provision rocprofv3 with the matching ROCm release if it is absent. It is not
required by the native recorder. Finalized JSON does not expose the SDK's
dropped-record accounting and cannot establish that every worker was recorded;
it is not sufficient evidence for skipping tests.

## Complementary tools

| Tool | Useful evidence | Role in selection |
| --- | --- | --- |
| [ROCProfiler SDK](https://rocm.docs.amd.com/projects/rocprofiler-sdk/en/latest/index.html) | Buffered dispatches, code-object symbols, drop counters | Native recorder described above |
| [ROCTx](https://rocm.docs.amd.com/projects/rocprofiler-sdk/en/latest/how-to/using-rocprofv3.html) | Named application regions and markers | Correlate test phases with kernels; preserve raw names and avoid `--kernel-rename` for source joins |
| RCCL API tracing (`rocprofv3 --rccl-trace`) | Collective API names and correlation IDs | Diagnose which collective launched generic RCCL kernels |
| [AMD SMI](https://rocm.docs.amd.com/projects/amdsmi/en/latest/index.html) and `rocminfo` | Device architecture, visibility, and interconnect topology | Explain which hardware a recording exercised |
| [ROCm Systems Profiler](https://rocm.docs.amd.com/projects/rocprofiler-systems/en/latest/index.html) | CPU stacks, runtime timelines, communication and system activity | Targeted investigation of startup, scheduling, or worker gaps |
| [ROCm Compute Profiler](https://rocm.docs.amd.com/projects/rocprofiler-compute/en/latest/index.html) | Hardware counters, memory hierarchy and kernel bottlenecks | Targeted kernel performance investigation |

Use `librocprofiler-sdk-roctx.so` or the SDK's `roctx` Python bindings for annotations.
Do not interpret sampled stacks, counters, or topology as proof that a test never
executed a kernel. Run richer profiling separately from coverage collection;
extra tracing and counter replay can materially change workload timing.
