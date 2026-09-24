# recorders

What each CI step actually ran, for the test selector. Both are off unless the build sets their switch. Meant for nightly and post-merge builds.

| | records | switch | writes |
| --- | --- | --- | --- |
| [`fnrec/`](fnrec) | Python functions entered | `VLLM_CI_FNREC=1` | `.fnrec/<job-id>/` |
| [`kernrec/`](kernrec) | CUDA kernels launched | `VLLM_CI_KERNREC=1` | `.kernrec/<job-id>/` |

Independent: turn on either, or both. Each owns its own directory, so neither can touch the other's files and the order they run in does not matter.

## Turning them on

Set these on the build, then run it:

```
VLLM_CI_FNREC=1                  # Python
VLLM_CI_KERNREC=1                # CUDA, plus the line below
VLLM_KERNEL_SYMBOL_MAP=1         # maps kernel symbols back to source files
```

Optional:

```
VLLM_CI_BRANCH=<ci-infra branch>            # which branch the payloads come from
VLLM_CI_ONLY_STEP_KEYS=["kernels-core-operation-test"]   # a few steps only
```

`VLLM_CI_BRANCH` defaults to `main`. Set it to test a recorder from a branch, or the build will fetch the payload from `main` regardless of what you changed.

## Where the data goes

**kernrec** appends a collect step that folds every job's recordings into one table and publishes it, with the symbol map, to `s3://vllm-ci-selector/<pipeline>/<commit>/`. `ci-fetch-kernel-record` pulls the latest published pair into `coverage-data/`.

**fnrec** is folded by the same collect step, which the generator appends when either recorder is on. It builds the Python table from the build's own artifacts, with no Buildkite token: each job's `fnrec.json` (written by `pack.sh` with the step's exit status) says which step it was and whether it passed, and the `fnrec_pytest` plugin's `pytest.*.txt` say whether its tests ran. The table ships as an artifact of the collect step, and goes to S3 beside the kernel pair when one is published. The same table can still be built offline:

```bash
export BK_TOKEN=...
ci-fetch-build https://buildkite.com/<org>/<pipeline>/builds/<n> --out sweeps/
ci-build-table <vllm-repo> sweeps/<org>-<pipeline>-<n> -o table.json.gz
```

## Coverage

Neither records multi-node steps or docker builds. kernrec is CUDA only, so AMD and TPU steps fall back to the static map. fnrec covers AMD, and covers plugin-less steps through a per-job `PYTHONPATH` install.

## Adding a third

The convention is **directory, switch, file prefix and output directory all share a name**: `fnrec/` is `VLLM_CI_FNREC`, writes `fn.*.txt` into `.fnrec/`. A recorder owns its own directory outright and never touches another's.

Payloads are fetched by `curl` at step time, so they stay stdlib-only and must run on the oldest Python an agent has. Nothing here imports the `ci_selector` package, and the generator references a recorder only as a URL.
