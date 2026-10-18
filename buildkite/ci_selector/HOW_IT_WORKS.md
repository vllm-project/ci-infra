# How CI picks the jobs for your pull request

vLLM's CI has a few hundred jobs. Most pull requests only affect a small part of the code, so running all of them on every PR wastes hours of GPU time and slows everyone down. The **ci-selector** decides which jobs your PR needs.

Before the ci-selector, each job carried a hand-written list of paths (`source_file_dependencies`): "run this job if any of these paths changed". Those lists drift as the code moves. A stale entry silently skips a test that should have run; a broad one (`vllm/`) runs a test on nearly every change. The ci-selector does not read those lists. It works out the answer from the code itself and from what each job was observed to run.

## What it looks at

**The code.** On every PR it reads the repository at the commit your branch started from: Python imports, the registries vLLM looks things up in by name (models, attention backends, quantization methods, ...), which tests each CI job runs, and how the Docker images are built. From the files you changed it works out every job that *could* be affected. Whenever it cannot work something out, it selects more, never less.

**What each job actually ran.** A scheduled full CI run on `main` records, for every job:

- which Python functions it called, and
- which CUDA kernels it launched, joined to the `csrc/` file each kernel was compiled from.

These recordings are published automatically after the run. They let the selector be precise in both directions:

- if a job was seen running a function or kernel your PR changed, it **runs**, even if the code analysis missed it;
- if a job's recording shows it never touched any of the code your PR changed, it is **dropped**, but only when every safety check agrees (the recording is complete and healthy, every changed file is one the recording can speak for, the code is not new, and so on).

A few examples of how that plays out:

| your change | what runs |
| --- | --- |
| a fix inside one Python function | the jobs that call that function, plus the jobs that run the test files beside it |
| a fix inside one CUDA kernel in `csrc/` | the jobs that launched that kernel |
| only comments, docstrings or type annotations | nothing beyond the image builds |
| `setup.py`, `CMakeLists.txt`, a Dockerfile | everything |
| something it cannot analyze (a file that does not parse, a dynamic import it cannot follow) | everything |

## What you see on your PR

- An annotation at the top of the Buildkite build: **ci-selector chose N test steps**, next to how many the old rules would have run.
- The jobs it picked run as usual. Image builds and other prerequisites come along automatically.
- Every job it did not pick is still listed under **Run more jobs**, near the top of the build. Open it, tick the jobs you want and submit; they run on the same build, with anything they depend on. Use it whenever you want extra coverage, no need to ask anyone.
- The **`ready-run-all-tests`** label runs everything, as before.

AMD jobs are not picked by the selector for now: they run on the same `source_file_dependencies` rules as before. Builds on `main`, release branches and the nightly runs are not affected: they run everything.

## If it skipped something it should not have

1. Add the job to your build with **Run more jobs**.
2. Tell us in `#sig-ci` on [vllm-dev.slack.com](https://vllm-dev.slack.com), or open an issue in [vllm-project/ci-infra](https://github.com/vllm-project/ci-infra/issues), with the build link and the job that should have run. A missed job is the bug we care about most.

Every PR build saves the full decision as a build artifact, `.buildkite/ci_selector_decision.json`. It lists what the old rules would have run, what the selector picked, what was scheduled, which recordings it used, and for each job the reasons it was picked or dropped. That file is usually enough for us to find the cause without re-running anything.

## When it does not decide

The selector steps aside, and the old rules apply, whenever:

- it fails, times out, or names a job the pipeline does not have;
- the build already names its jobs (`VLLM_CI_ONLY_STEP_KEYS`, used by retry builds);
- the build is on `main`, a release branch, or the nightly.

It never makes a build fail.

## Running it yourself

The selector lives in [`buildkite/ci_selector`](.) in `vllm-project/ci-infra`. With a vLLM checkout:

```bash
cd buildkite/ci_selector
uv sync
uv run ci-fetch-function-record   # the Python recording
uv run ci-fetch-kernel-record     # the CUDA kernel recording
uv run ci-select --repo /path/to/vllm --diff origin/main...HEAD --emit-keys
```

Drop `--emit-keys` to see every selected job with the reasons it was picked. [`README.md`](README.md) covers the commands, and [`DESIGN.md`](DESIGN.md) the design and its safety rules.
