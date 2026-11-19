# kernrec: which GPU kernels did this step launch?

The CUDA recorder uses CUPTI. [ROCm collection](ROCM.md) uses the ROCProfiler
SDK with the same per-process recording and row-health contract.

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
  KERNREC_DIR: .kernrec/$BUILDKITE_JOB_ID      # default when unset
commands:
  - pytest -v -s tests/kernels/core          # unchanged
```

Every child process inherits the variable, so tensor-parallel workers and the
engine-core process behind `vllm serve` record themselves into their own
`kern.<pid>.txt`. When the variable is unset nothing loads: PR jobs pay
nothing. Recording is meant for nightly or post-merge full runs only.

Each name is written when its kernel is launched or added to a CUDA graph,
before it can run: CUPTI driver-API callbacks look the handle's name up
(`cuFuncGetName`, or `cuKernelGetName` for `CUkernel` handles) and write it
with one `write(2)` the first time it is seen. The header says `sync=1`, and
the file is complete even if the process is killed or leaves through
`os._exit`. The callbacks are enabled all or nothing; when the driver lacks
the lookups (before CUDA 12.3) or CUPTI refuses one, the header omits
`sync=1` and completeness rests on the footer.

The Activity API runs alongside as the cross-check, flushed on a timer
(`KERNREC_FLUSH_MS`, default 1000, 0 turns it off; forced every
`KERNREC_FORCE_EVERY` ticks, default 5). A name it delivers that no callback
wrote is logged as `# error=callback-missed <name>`, then the name. The file
is opened at `cuInit`, so a process that dies before launching anything
still leaves a header.

Under the docker plugin the step runs as root and the checkout is a bind
mount owned by the agent user, so anything left there with default modes
is something the agent's `git clean` can never remove, and every later job
on that machine fails at checkout (this happened once, build 89825). Both
layers guard against it: `ci_setup.sh` creates `.kernrec/` and the job
directory `0777` and opens up everything on `EXIT`, and the recorder itself
chmods any directory it has to create. `test_checkout_perms.sh` reproduces
the conditions in Docker and checks both, including a SIGKILLed step.

## Output

```
# kernrec v1 backend=cuda recorder=cupti activity=1 sync=1 pid=4242 ppid=4200 exe=/usr/bin/python3
# gpu_active
_Z21fusedQKNormRopeKernelIN3c108BFloat16ENS0_ELi128ELb1EEvPvT_...
fused_moe_kernel
_ZN9deep_gemm...
# end records=183220 unique=311 dropped=0 errors=0 unresolved=0
```

- `# gpu_active`: the first CUDA context was created. A file without it
  launched nothing.
- `# dropped=N`: CUPTI buffer overflow. A row with drops is never trusted.
- `# error=...`: anything that could hide a launch (`callback-missed`,
  `callback-unresolved`, `released-to-profiler`, a failed write or flush).
- `# end`: the footer, written at normal exit. Optional with `sync=1`, but
  it must agree with the file when present.

`kernel_table.py` reads a `sync=1` file with no error lines and a whole last
line as complete, for both backends (ROCm names kernels from the SDK's enqueue
callback; see ROCM.md). Other files need the footer or must be idle. The
table's stats print `callback_missed` and `rows_usable_footer_rule` (the
footer rule alone).

## What it sees

Everything that reaches the driver, however the binary was made: ahead-of-time
`_C` kernels, Triton (`@triton.jit` functions appear under their Python name),
DeepGEMM and FlashInfer JIT, cuBLAS and cuDNN, and kernels replayed from CUDA
graphs (named when the graph is built).

Known limits:

- One CUPTI activity client and one callback subscriber per process. When a
  CUDA `torch.profiler` prepares, `kernrec_torch.py` calls
  `kernrec_release()`, which hands both over and marks the file
  `# error=released-to-profiler`.
  Any other CUPTI client (Triton Proton, Nsight, a CUPTI timer) cannot
  subscribe while a recording process holds the callback slot.
- Kernels launched from device code (dynamic parallelism, device graph
  launch) never reach a host callback. The Activity API reports them as
  `callback-missed` in a process that lives to a flush; in a process killed
  first, they are lost.
- A write that fails empties the file, so it reads as incomplete.
- Both backends require every observed process to finish cleanly, with no
  drops, errors, or unresolved names, before the row may authorize a drop.
  Partial recordings still provide positive evidence. Neither backend proves
  that every expected worker inherited the recording environment.
- The [ROCm SDK recorder](ROCM.md) is the AMD counterpart. TPU has no recorder.

## Running it in CI

The pipeline generator arms it when the build has `VLLM_CI_KERNREC=1`:
every GPU step gets a setup command that sources `ci_setup.sh` from the
ci-infra branch that generated the pipeline (`VLLM_CI_BRANCH`, default
`main`), and `artifact_paths: [".kernrec/**/*"]`. The setup script fetches the
prebuilt `libkernrec.so` for CUDA, or builds the ROCm tool against the image's
pinned SDK, exports the injection variables,
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

## Collecting and publishing

When a build records, the pipeline generator appends one last step,
`kernrec-collect`, that depends on every command step (blocked ones
excluded, so it can never wait on a step that will not start) and runs
whether they passed or failed. It executes `collect.sh`:

1. `buildkite-agent artifact download ".kernrec/**/*"` pulls every job's
   recordings and the `kernrec.json` sidecar (step key, shard, exit status),
   so no Buildkite API token is needed. `ci_setup.sh` writes it at setup with
   a null status and the generator's last command, `kernrec_finish`, rewrites
   it with the real one. Not an EXIT trap alone: vLLM's OTel prelude installs
   its own and replaced ours, which read a whole nightly as failed once.
2. `kernel_table.py build --kernrec .kernrec --backend cuda` (or `rocm`) folds
   each backend into one row per step:
   kernels launched, jobs, all-passed, processes, dropped records.
3. The table and the build's `kernel_symbol_map.json.gz` are uploaded as
   artifacts of the collect job. Then, only if both validate (rows in the
   table, objects and no failure reason in the map) and the agent has AWS
   identity, to

   ```
   s3://vllm-ci-selector/<pipeline>/kernrec/<commit>/kernel_table.json.gz
   s3://vllm-ci-selector/<pipeline>/kernrec/<commit>/kernel_symbol_map.json.gz
   s3://vllm-ci-selector/<pipeline>/kernrec/latest.json
   ```

   Its own prefix, beside the function record's. Each recorder owns its
   tree here as it does in the checkout.

   The commit prefix is written as a unit and `latest.json` last, so a
   consumer following the pointer always finds a complete, validated pair.
   Several builds can collect the same commit (daily and nightly often
   share one); a later run whose map is broken publishes nothing and leaves
   the earlier pair in place. The bucket is public-read
   ([bucket configuration](https://github.com/vllm-project/ci-infra-private/blob/main/terraform/aws/s3.tf), maintainer access required), so the selector's bootstrap can fetch over plain
   HTTPS; the postmerge agents that run the collect step hold the write
   policy defined in the [private infrastructure configuration](https://github.com/vllm-project/ci-infra-private/blob/main/terraform/aws/iam.tf). The step is `soft_fail`: a publishing
   problem is reported, never a red build.

## Consuming it

`ci-fetch-kernel-record` (in `ci_selector/scripts/`) pulls the latest
published CUDA pair into `coverage-data/`; `--backend rocm` fetches its independent
pair. The selector reads both by default, and `ci_selector/coverage/kernels.py`
reads it inside `decide.py`: a step whose row launched a kernel compiled from
a changed csrc file is selected, a step whose healthy row launched none is
dropped when that file was all the map had on it, and everything else falls
to the map. The rule and its gates are in that module's docstring. First
measured on PR 55755: 311 jobs from the map, 158 with the record.

Legacy CUDA tables and traces can select tests but cannot justify skips;
refresh them with the updated recorder and table builder.

## Not here yet

Wiring `ci-select` into the Buildkite bootstrap. Nothing in CI calls the
selector today (see `DESIGN.md` section 7).
