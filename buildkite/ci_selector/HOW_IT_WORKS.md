# How CI picks jobs for your pull request

**ci-selector** uses your diff, the source code, and recordings of previous CI runs to choose which jobs to run. It replaces hand-written `source_file_dependencies` rules for non-AMD jobs.

## The main components

The selector combines a static code map with three pieces of execution evidence:

| Component | What it tells us |
| --- | --- |
| **Static code map** | Traces imports, registries, test commands, and image dependencies to find jobs a change could affect, including code without recordings. |
| **Kernel source map** | During image builds, maps compiled kernel symbols back to their source files in `csrc/`, including `.cu` files and headers. |
| **Python recorder** | During instrumented CI jobs, records which Python functions each job calls: **job → function**. |
| **CUPTI kernel recorder** | During instrumented NVIDIA CI jobs, records which GPU kernels each job launches: **job → kernel**. |

Joining the kernel recordings with the source map gives **job → kernel → source file**. Together with the Python recordings, this connects jobs to the code they actually executed.

## How a PR becomes a job list

1. **Read the diff.** Compare the PR against its merge base and identify changed files, Python functions, and kernels where possible.
2. **Propose jobs from the code map.** Follow the affected dependencies, accounting for new code and connections introduced by the PR.
3. **Refine with execution evidence.** Use recordings from previous CI runs to add jobs that exercised changed code and remove candidates that did not. Removing a job requires a healthy recording and evidence covering every reason it was selected. When evidence is insufficient, keep the code map's selection.

For example, changing a CUDA kernel connects the diff to its compiled symbols, then to jobs that launched those kernels. Changing a Python function connects directly to jobs that called it. Neither is an exclusive list: nearby tests and jobs the records cannot safely exclude can still run.

## What runs in CI

The selector runs on PR and branch builds; `main` builds compare against their parent commit. Image builds and other prerequisites are included automatically. **AMD jobs still use the old path-based rules.**

Release, nightly, run-all, docs-only, and explicitly selected builds (`VLLM_CI_ONLY_STEP_KEYS`) keep the pipeline's existing rules. Selector failures, timeouts, or unknown job keys also fall back to those rules.

`VLLM_CI_SELECTOR=shadow` records a decision without applying it; `off` disables the selector.

## Check or override a decision

- **Buildkite annotation:** shows the selected job count alongside the old rules' count.
- **Run more jobs:** select skipped jobs to run on the same build, with their prerequisites.
- **`ready-run-all-tests` label:** request a full run.
- **`.buildkite/ci_selector_decision.json` artifact:** records the old, selected, and scheduled job lists, the recordings used, per-job explanations, and any fallback.

If a needed job was skipped, run it through **Run more jobs**, then report the build link and job in `#sig-ci` on [vLLM Slack](https://vllm-dev.slack.com) or a [ci-infra issue](https://github.com/vllm-project/ci-infra/issues).

## Run locally

From this repository, with a separate vLLM checkout:

```bash
cd buildkite/ci_selector
uv sync
uv run ci-fetch-function-record
uv run ci-fetch-kernel-record
uv run ci-select --repo /path/to/vllm --diff origin/main...HEAD --emit-keys
```

Drop `--emit-keys` to include selection reasons. See [README.md](README.md) for commands and [DESIGN.md](DESIGN.md) for the selection rules.
