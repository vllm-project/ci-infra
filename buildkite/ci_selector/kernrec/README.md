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

## Not here yet

The build-side symbol map (`cuobjdump -symbols` over the `.cu.o` files plus
`ninja -t deps` for headers, produced in the Dockerfile build stage), the
table type in `ci_selector/coverage`, and the csrc decision rule in
`decide.py`. See the plan in the PR that adds this directory.
