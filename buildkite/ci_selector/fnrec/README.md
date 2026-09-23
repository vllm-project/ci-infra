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
counted in `other`. `#end` is the clean-exit marker a killed engine core
never writes; the table's stamp counts how many processes did.

## Building the table

Offline, as `ci_selector/scripts/README.md` describes: `ci-fetch-build`
downloads a build's `.fnrec/**` artifacts and job logs (Buildkite API
token), `ci-build-table` merges them into `coverage-data/table.json.gz`.
Doing that inside the recording build's collect step, the way the kernel
table is built, needs either an API token on the postmerge agents or a
log-free stamp, and is not here yet.

## Tests

`tests/coverage/test_fnrec_producer.py` runs the recorder in a subprocess
against a fake `vllm` package and reads the result back with the real
`coverage/model.py` reader: names, DISABLE, fork, outside-root counting,
the off switch, and a taken tool slot.
