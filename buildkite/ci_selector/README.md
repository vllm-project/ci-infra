# ci-selector

Works out which vLLM CI jobs a diff needs to run. It derives the answer from the source tree and the Buildkite config, then checks that against a record of what each CI step actually executed.

## How it works

**The code map** reads the repo and the CI config (import graph, registries, step targets, container-build DAG) and works out which steps a diff could affect. When it cannot work something out it selects more, never less.

**The coverage record** is a table of what each step actually ran on real CI builds, one row per step, produced by an instrumented build. The recorder that produces it is `recorders/fnrec/`, loaded into every Python process of a step when the build has `VLLM_CI_FNREC=1`; `scripts/` turns a build's recordings into the table.

**The kernel record** is the same idea for `csrc/`, where no Python frame exists: a table of the GPU kernels each step launched (CUPTI, recorded on the nightly and daily runs) joined to a map of which csrc file each kernel was compiled from (read off the image build's objects). Both are produced by `recorders/kernrec/`.

Neither is a stage of the other. `decide.py` reads all of them, per changed file:

| for file F and step S | decides |
| --- | --- |
| S has a row, and it shows S ran F (a function, or a kernel compiled from F) | **select**, and the map gets no vote |
| S has a row, and it shows S ran none of F | **drop**, if every gate agrees |
| S has no row, or F is outside what the record can see | **the map decides** |

A changed `.cu` votes with the kernels its diff touched, not the whole file: `coverage/changed_kernels.py` reads both sides of the file, finds the definition each changed line falls in (a `__global__`, a `__device__` helper and the kernels reaching it, a host launcher and the kernels it launches, a constant and the functions using it) and joins the names back to the map's symbols. Anything it cannot name falls back to the whole file. `CI_SELECTOR_KERNEL_ATTRIBUTION=file` restores the whole-file reading for measurement.

Selecting takes one observation and carries no gate. Dropping carries all of them. For the kernel record the gates are: the row is healthy (every job passed, every shard reported, no dropped records), the file is clearable (compiled into kernels and into no host-only object, so a header `torch_bindings.cpp` includes may select but never drop), the step was selected for nothing but files the record can clear, no step declares the file by name, and the table and map come from the same commit.

## Setup

Needs a local vLLM checkout to analyze against.

```bash
uv sync
source .venv/bin/activate
```

Both coverage records are fetched the same way, into `coverage-data/`:

```bash
ci-fetch-function-record        # the Python record
ci-fetch-kernel-record          # the kernel record, which answers for csrc/
```

Each follows its own `latest.json` in the public `vllm-ci-selector` bucket, validates what it downloaded, and only then replaces what is on disk.

Override the locations with `--table` or `$CI_SELECTOR_TABLE`, and `--kernel-table` / `--kernel-symbol-map` or `$CI_SELECTOR_KERNEL_TABLE` / `$CI_SELECTOR_KERNEL_SYMBOL_MAP`. Without the Python record the selector runs on the code map alone and says so on stderr; without the kernel record csrc files do the same, which today means every step on the CUDA image.

## Commands

```bash
# Both inputs. This is the answer CI would use.
ci-select --repo /path/to/vllm --diff origin/main...HEAD

# What the code alone says, for comparison and debugging.
ci-select codemap --repo /path/to/vllm --diff origin/main...HEAD
```

A two-ended range is required. `origin/main...HEAD` is the PR's merge-base diff, which is what CI sees. Output is JSON: the steps to run, why each was selected, and any run-all fallbacks.

### Step keys for CI

`--emit-keys` prints the selection as the key list Buildkite consumes, spelled the way the pipeline generator spells it.

```bash
ci-select --repo /path/to/vllm --diff origin/main...HEAD --emit-keys
```

### Crosscheck

Replays real PRs and compares our selection against what CI actually ran, and what failed. Needs `gh`.

```bash
ci-validate crosscheck --repo /path/to/vllm --prs 50378 47189
```

Each line reads CI ran / code map alone / records + code map, and `kern +a/-d` is the kernel record's share. `--kernel-table` and `--kernel-symbol-map` point it at a specific pair; `CI_SELECTOR_KERNEL_UNMATCHED_DROPS=1` lets a table and map from different commits drop steps, which is for measuring against older data and never for production.

Run it after any change to selection. It exits 1 on a problem, and also when it finds nothing at all, because a detector that has stopped detecting looks like a clean result from the outside. Anything checkable from a plain checkout is a drift-marked test instead, see Tests below.

### Leak replay

`test-selection/selection-leaks.json` at the repository root is the curated corpus of confirmed selection leaks: jobs that did not run on a pull request and then failed on main because of it. Replaying it asks the recall question crosscheck cannot: would we have reached the job that broke?

```bash
ci-validate leaks --repo /path/to/vllm
```

Each leaked job scores as `selected` (in the emitted selection, so it runs), `optional reached` (a rule reached it, but the step is optional and the emitter leaves optional steps out; the generator would run it if named, so this is a policy choice), or `missed`. Today's rules score zero on this corpus by construction. Re-run it after any change to selection; the counts must never fall.

### Shadow mode

The selector running on real PR builds next to today's CI, gating nothing (DESIGN.md §8). Off unless the build has `VLLM_CI_SELECTOR_SHADOW=1`, set on the `ci` pipeline's environment for every PR build or on a single build's environment for a trial. On a non-main build the generator then adds one step, `ci-selector-shadow` (`:crystal_ball: CI selector (shadow)`): small premerge CPU queue, `soft_fail`, no dependencies, and nothing waits on it. It runs [`shadow/run.sh`](shadow/run.sh) from the generating branch (`VLLM_CI_BRANCH`, default `main`): merge-base of the PR head with vLLM main, the latest published records, `ci-select --emit-keys`. The answer lands as the `shadow/selection.json` artifact, with `shadow/selector.log`, and a `ci-selector-shadow` annotation.

```
VLLM_CI_SELECTOR_SHADOW=1
VLLM_CI_BRANCH=<ci-infra branch>   # only to try an unmerged run.sh
```

Scoring finished builds needs a Buildkite token with `read_builds` and `read_artifacts`:

```bash
BUILDKITE_TOKEN=... ci-validate shadow --builds 91500 91501 --json-out shadow.json
BUILDKITE_TOKEN=... ci-validate shadow --prs 55755 53280   # latest finished build of each head
```

Per build: jobs today ran against the ones the selection keeps, and each failure in a step it would skip as `flake` (passed on a retry in the same build), `pre-existing` (the same step key failed on a main build within `--window-hours`, default 48, of the PR's base commit), or `MISS`. `--repo` reads base commit times from a local checkout instead of `gh`. Kept steps' generator-added dependencies are not in the artifact, so a failure in one of those reads as would-skip: the count errs toward MISS. `shadow/test_run.sh` tests the payload under a mocked agent.

## Tests

```bash
# All tests. VLLM_REPO is required; VLLM_PIN holds the commit we are green against.
VLLM_REPO=/path/to/vllm uv run pytest tests -q

# Just the drift guards: the ones that fail when vLLM moved under us, or the
# generator beside us did, rather than when our code is wrong.
VLLM_REPO=/path/to/vllm uv run pytest tests -m drift -q
```

A `drift` failure means a hardcoded fact went stale. Usually the fix is editing `handwritten.py` or teaching a parser; for one of the values we re-export from the generator, it is editing the generator's own `amd.py`. `VLLM_REPO=/path/to/vllm pytest tests -m drift --collect-only -q` lists what is watched.

`tests/` covers the code map and needs a real vLLM checkout, named by `VLLM_REPO`. `tests/coverage/` covers the coverage half and builds throwaway repos, so it needs nothing.

## The pin

`VLLM_PIN` holds one vLLM commit. It means **this version of the tool is green against that version of vLLM**.

**It only ever moves as part of a repair.** Teach the parser, then advance the pin, in one commit. A bump on its own is a claim nobody checked, and it silently turns a guard that was protecting you into one that is just green.

It has to be a commit reachable in `vllm-project/vllm`, because CI clones that repo and checks it out.
