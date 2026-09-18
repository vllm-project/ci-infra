# ci-selector

Works out which vLLM CI jobs a diff needs to run. It derives the answer from the source tree and the Buildkite config, then checks that against a record of what each CI step actually executed.

## How it works

**The code map** reads the repo and the CI config (import graph, registries, step targets, container-build DAG) and works out which steps a diff could affect. When it cannot work something out it selects more, never less.

**The coverage record** is a table of what each step actually ran on real CI builds, one row per step, produced by an instrumented build.

Neither is a stage of the other. `decide.py` reads both, per changed file:

| for file F and step S | decides |
| --- | --- |
| S has a row, and it shows S ran F | **select**, and the map gets no vote |
| S has a row, and it shows S ran none of F | **drop**, if every gate agrees |
| S has no row, or F is outside the recorder's root | **the map decides** |

Selecting takes one observation and carries no gate. Dropping carries all of them.

## Setup

Needs a local vLLM checkout to analyze against.

```bash
uv sync
source .venv/bin/activate
```

For the coverage half, put a table at `coverage-data/table.json.gz`, which is gitignored because it is a build artifact. Override the location with `--table` or `$CI_SELECTOR_TABLE`. Without one, the selector runs on the code map alone and says so on stderr.

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

Run it after any change to selection. It exits 1 on a problem, and also when it finds nothing at all, because a detector that has stopped detecting looks like a clean result from the outside. Anything checkable from a plain checkout is a drift-marked test instead, see Tests below.

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
