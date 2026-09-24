# fnrec: which Python functions did this step run?

Records the set of vLLM functions a CI step entered, one qualified name per
line. It is the Python-side twin of the kernel recorder: that one says which
GPU kernels a step launched, this says which Python it executed. The selector
uses them to drop steps that provably ran none of the changed code.

## How it runs

Subscribes to CPython's `sys.monitoring` PY_START event and returns `DISABLE`
from the callback, so each function costs one event in the life of a process.
It starts on the first `vllm` import, not at interpreter startup, so other
infrastructure in the image is left alone.

Two install modes, chosen by the generator because only it knows the step:

- **container** — `fnrec.py` plus a one-line `.pth` into the interpreter's
  site-packages. The container is thrown away with the step.
- **host** — a plugin-less step runs on the agent itself, where a `.pth` would
  load for every later job on that machine. Installs into a per-job directory
  on `PYTHONPATH` instead, which the next checkout removes.

Either way every Python process the step starts records itself, so pytest,
engine cores and TP or Ray workers are all covered.

## Output

`<checkout>/.fnrec/<job-id>/fn.<host>.<nonce>.<pid>.txt`, one file per process:

```
#start	pid=72016	ppid=71967	host=...	nonce=21a82beb	origin=import	root=vllm/	...
#root	vllm/	t=1790190284.827
vllm/__init__.py	<module>	1
vllm/__init__.py	Engine	5
vllm/__init__.py	Engine.start	6
vllm/__init__.py	top	1
#end	root=4	other=247	errors=0	last_error=	t=1790190284.827
```

`#`-prefixed lines are metadata. The header carries process and job identity
and a short allowlist of environment values; names are listed one by one, never
by prefix, because this file is uploaded and `BUILDKITE_*` or `HF_*` would sweep
up access tokens.

`#end` marks a clean exit. Its absence means the process was killed, which the
table builder counts rather than ignores. `errors` counts functions seen but not
written, so a thinned record is visibly thin.

At the end of the step `pack.sh` folds the per-process files into
`<job-id>.tar.gz`. It uploads nothing; the step's `artifact_paths` does that,
and a second glob covers the raw files if packing never ran.

The kernel recorder writes to its own `.kernrec/`, so neither recorder can
reach the other's files and the order they run in does not matter.

## What it records

Any function under the vLLM package directory, by file and qualified name, so
fifty different `forward` methods stay distinct. Everything outside that
directory is counted but not written.

Known limits:

- Multi-node steps are not recorded. They run on several hosts with no plugin,
  and nothing scopes an install to one job across them.
- Docker build steps are not recorded. They run no vLLM code.
- A file that is opened rather than imported, such as a template or a config,
  is invisible: it executes no Python.

## Running it in CI

The pipeline generator arms it when the build has `VLLM_CI_FNREC=1`. Every
armed step sources `ci_setup.sh` from the ci-infra branch that generated the
pipeline (`VLLM_CI_BRANCH`, default `main`) and gets `artifact_paths` for its
output. Independent of `VLLM_CI_KERNREC`: either can be turned on alone.

To record a few steps from a branch under test:

```
VLLM_CI_BRANCH=<ci-infra branch>
VLLM_CI_FNREC=1
VLLM_CI_ONLY_STEP_KEYS=["kernels-core-operation-test"]
```

Unlike the kernel recorder this also covers AMD and plugin-less steps.

## The files

| | |
| --- | --- |
| `ci_setup.sh` | sourced at the start of a step; fetches the rest and exports the environment the recorder needs |
| `fnrec.py` | the recorder |
| `install.py` | container install: `fnrec.py` and a `.pth` into site-packages |
| `host_install.py` | host install: a per-job directory with `sitecustomize.py` |
| `pack.sh` | folds this job's files into one tarball at the end of the step |

## Turning recordings into the table

Folded offline after a recording build, then published so nobody else has to
repeat it. Tools are in `ci_selector/scripts`:

```bash
export BK_TOKEN=...
ci-fetch-build https://buildkite.com/<org>/<pipeline>/builds/<n> --out sweeps/
ci-build-table <vllm-repo> sweeps/<org>-<pipeline>-<n> -o table.json.gz
ci-publish-function-record table.json.gz
```

Everyone else runs `ci-fetch-function-record`, which needs no token. See
`ci_selector/scripts/README.md`.
