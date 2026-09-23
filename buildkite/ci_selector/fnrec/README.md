# fnrec: which vLLM functions did this step enter?

The producing half of the Python coverage record. `ci_selector/coverage`
has read rows in this shape since #555; this writes them. Its kernel-side
twin is `../kernrec/`.

## How it runs

`fnrec.py` subscribes to CPython's `sys.monitoring` PY_START event and
returns `DISABLE` from the callback, so each code object costs one event
in the life of the process. It is loaded into every Python process of a
step by a one-line `.pth` in site-packages (`import fnrec`) that
`ci_setup.sh` installs, and it does nothing unless `FNREC_DIR` is set.
Every process, the pytest runner, the engine core, tensor-parallel and Ray
workers, writes its own `fn.<pid>.txt`; a forked child starts a new file.

The pipeline generator arms it when the build has `VLLM_CI_FNREC=1`: every
GPU step sources `ci_setup.sh` from the generating ci-infra branch and
uploads `.fnrec/**/*`. The directory is shared with the kernel recorder, so
one artifact glob and one collect step cover both.

```
VLLM_CI_BRANCH=<ci-infra branch>
VLLM_CI_FNREC=1
VLLM_CI_ONLY_STEP_KEYS=["basic-correctness","kernels-core-operation-test"]
```

## Output

```
#start  pid=4242  root=/usr/local/lib/python3.12/dist-packages/vllm/  py=3.12.13  BUILDKITE_JOB_ID=...
#root   /usr/local/lib/python3.12/dist-packages/vllm/  t=1758600000
/usr/local/lib/python3.12/dist-packages/vllm/engine/llm_engine.py  LLMEngine.step  1
#stat   root=1000  other=812  errors=0  last_error=  t=1758600100
#end    root=4211  other=2077  errors=0  last_error=  t=1758600400
```

Only functions under the vLLM package directory are written; the rest are
counted in `other`. The file is created on the process's first vLLM function, so
the compile workers and helpers that never enter vLLM, 90% of a step's
Python processes, leave nothing. `#end` is the clean-exit marker a killed engine core
never writes; the table's stamp counts how many processes did.

## Test outcomes

A row built from a job whose tests all skipped holds little more than its
imports, so the table's stamp carries each job's pytest counts. Offline they
come off the Buildkite log. The collect step has no log, so `ci_setup.sh`
also installs `fnrec_pytest.py` as a pytest plugin (a `pytest11` entry point
in the same site-packages) that writes each run's two log lines the parser
reads, in pytest's own format, to `pytest.<pid>.txt`:

```
collected 12 items
========== 10 passed, 2 skipped in 3.21s ==========
```

The collection line is written first and on its own, so a run killed before
its summary reads as unparsed and keeps the row thin. `pytest.installed`
marks that the plugin was in place; without it the collect step treats the
job's counts as unknown.

## Building the table

Two ways, same output:

- In the recording build. The collect step (`kernrec/collect.sh`) runs
  `ci-sweep-from-artifacts` on the downloaded `.fnrec/`, then
  `ci-build-table` against the build's own checkout, and ships
  `table.json.gz` next to the kernel pair. No API token: identity and exit
  status come from each job's `kernrec.json`, so it needs `VLLM_CI_KERNREC=1`
  as well.
- Offline, as `ci_selector/scripts/README.md` describes: `ci-fetch-build`
  downloads the artifacts and job logs with a Buildkite token, then
  `ci-build-table`.

## Tests

`tests/coverage/test_fnrec_producer.py` runs the recorder in a subprocess
against a fake `vllm` package and reads the result back with the real
`coverage/model.py` reader: names, DISABLE, fork, outside-root counting,
the off switch, and a taken tool slot. `test_fnrec_pytest.py` runs real
pytest with the plugin and requires its lines and pytest's own terminal
summary to parse to the same counts. `test_sweep_from_artifacts.py` lays out
a build's artifacts, converts them and merges the result.
