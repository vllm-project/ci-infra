# kernrec: which GPU kernels did this step launch?

A CUPTI injection library that records the *set* of kernel names a process
launches, one mangled name per line. It is the kernel-side twin of the
Python function recorder: the Python record says which functions a CI step
entered, this says which kernels it ran. Together they let a change to a
`.cu` file select the steps that actually executed code from that file,
instead of every step that runs the CUDA image.

## How it runs

No wrapper. The CUDA driver loads the library itself when the environment
names it, and calls `InitializeInjection()` during `cuInit`:

```yaml
env:
  CUDA_INJECTION64_PATH: /opt/vllm-ci/libkernrec.so
  KERNREC_DIR: .fnrec/$BUILDKITE_JOB_ID      # default when unset
commands:
  - pytest -v -s tests/kernels/core          # unchanged
```

Every child process inherits the variable, so tensor-parallel workers and the
engine-core process behind `vllm serve` record themselves into their own
`kern.<pid>.txt`. When the variable is unset nothing loads: PR jobs pay
nothing. Recording is meant for nightly or post-merge full runs only.

Names are appended the first time they are seen, so a test killed by a
timeout still leaves everything it launched up to that point.

Under the docker plugin the step runs as root and the checkout is a bind
mount owned by the agent user, so anything left there with default modes
is something the agent's `git clean` can never remove, and every later job
on that machine fails at checkout (this happened once, build 89825). Both
layers guard against it: `ci_setup.sh` creates `.fnrec/` and the job
directory `0777` and opens up everything on `EXIT`, and the recorder itself
chmods any directory it has to create. `test_checkout_perms.sh` reproduces
the conditions in Docker and checks both, including a SIGKILLed step.

## Output

```
# kernrec v1 pid=4242 ppid=4200 exe=/usr/bin/python3
_Z21fusedQKNormRopeKernelIN3c108BFloat16ENS0_ELi128ELb1EEvPvT_...
fused_moe_kernel
_ZN9deep_gemm...
# dropped=0
# end records=183220 unique=311 dropped=0
```

`# dropped=N` marks CUPTI buffer overflow: launches we never saw. A row with
drops should be treated as suspect by the table builder, not trusted.

## What it sees

Everything that reaches the driver, however the binary was made: ahead-of-time
`_C` kernels, Triton (`@triton.jit` functions appear under their Python name),
DeepGEMM and FlashInfer JIT, cuBLAS and cuDNN, and kernels replayed from CUDA
graphs (CUPTI reports one record per graph node).

Known limits:

- One CUPTI activity client per process. If `torch.profiler` (Kineto) starts,
  it takes over the buffer callbacks and this recorder goes quiet. Steps that
  run profiler tests should not record.
- CUDA only. ROCm has an equivalent hook (`ROCP_TOOL_LIBRARIES`), not written
  yet. TPU has nothing. Those steps stay on the static map.

## Running it in CI

The pipeline generator arms it when the build has `VLLM_CI_KERNREC=1`:
every GPU step gets a setup command that sources `ci_setup.sh` from the
ci-infra branch that generated the pipeline (`VLLM_CI_BRANCH`, default
`main`), and `artifact_paths: [".fnrec/**/*"]`. The setup script fetches the
prebuilt `libkernrec.so` from the same branch, exports the variables above,
and never fails the step. To record a few steps from a branch under test:

```
VLLM_CI_BRANCH=<ci-infra branch>
VLLM_CI_KERNREC=1
VLLM_CI_ONLY_STEP_KEYS=["fusion-e2e-quick-h100","kernels-core-operation-test"]
```

## Build

The CI test image is `nvidia/cuda:*-base` and has no compiler, so the
library is prebuilt and committed here. `cross-build.sh` reproduces it from
any machine, no CUDA needed: zig cross-compiles to x86_64 glibc 2.28 against
the CUPTI and CUDA headers from NVIDIA's pip wheels and apt packages, pinned
to the CUDA minor the image runs. `build.sh` is the native variant for a box
that has the toolkit or torch's CUPTI wheel. Rebuild when the image moves to a
new CUDA major.

## Spike

```bash
VLLM_DIR=/path/to/vllm ./spike.sh /tmp/kernrec-spike
```

Seven checks on one CUDA box (two GPUs for check E): direct kernel test, the
same kernel from graph replays in a fusion e2e run, Triton, DeepGEMM, TP=2,
overhead, and the profiler conflict. The acceptance case is vLLM PR #55755:
after the recorder is in nightly, a change to
`csrc/libtorch_stable/fused_qknorm_rope_kernel.cu` should select only the
steps whose recorded set contains `fusedQKNormRopeKernel`, roughly a dozen
instead of 225.

## Spike result (Buildkite build 89825, 2026-09-18)

Six steps recorded from ci-infra branch `kernrec-spike` against vLLM main
`3263658`. Every job recorded, zero dropped records, and the target kernel
appeared exactly where it should and nowhere else:

| step | procs | kernels | fusedQKNormRope | Triton fused_moe | deep_gemm |
|---|---|---|---|---|---|
| fusion-e2e-tp2-quick-h100 | 27 | 426 | 3 | 1 | 0 |
| kernels-core-operation-test (3 shards) | 9 | 390 | 4 | 0 | 0 |
| pytorch-compilation-passes-unit-tests | 1 | 221 | 4 | 0 | 0 |
| kernels-deepgemm-test-h100 | 4 | 330 | 0 | 1 | 161 |
| kernels-moe-test (5 shards) | 9 | 838 | 0 | 2 | 6 |

The 27 processes in the TP2 step are the engine cores and tensor-parallel
workers across the tests, all recorded through the inherited environment
variable with no wrapper. Job durations were within noise of the previous
main build (−6% to +10%, single samples). Reproduce the scoring with
`analyze_build.py 89825` and the expectations listed in its docstring.

## The other half: kernel symbol → source file

Recordings say which kernels a step launched. The kernel symbol map says
which source file each kernel came from, read from the objects the image
build actually compiled. It is produced on the vLLM side
(`tools/ci/kernel_symbol_map.py`, run in the `csrc-build` Dockerfile stage
when the build arg `VLLM_KERNEL_SYMBOL_MAP=1`) and exported by the
`kernel-symbol-map` bake target in `docker/ci.hcl`, which `image_build.sh`
uploads as the artifact `kernel_symbol_map.json.gz` on the image-build job.
Per object: source, CMake target, kernel entry symbols from
`cuobjdump -symbols`, and the headers from `ninja -t deps`, so a header
change reaches every kernel compiled with it.

`join_symbol_map.py <map> <recordings-dir> --file csrc/x.cu` joins the two
and prints the steps a change to that file would select. First real run
(Buildkite build 90006 against the 89825 recordings): 569 objects, 10,447
kernel symbols, 62 s inside the cached build. For the #55755 file it
selected fusion-e2e-quick, fusion-e2e-tp2-quick, core-operation-kernels and
compilation-passes and dropped moe and deepgemm; `csrc/cuda_compat.h`
selected all six, as a header included everywhere should.

## Not here yet

The table type in `ci_selector/coverage` that stores per-step kernel sets
plus the map per commit, and the csrc decision rule in `decide.py`.
