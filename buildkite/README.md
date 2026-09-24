# buildkite

The tools that generate and select vLLM's CI, as one uv workspace.

| | |
| --- | --- |
| `pipeline_generator/` | turns the CI config into a Buildkite pipeline |
| `ci_selector/` | decides which steps a change needs |
| `bootstrap.sh` | the first thing a build runs: installs uv, fetches this repo, builds the environment, generates the pipeline |
| `bootstrap-amd.sh`, `bootstrap-intel.sh` | the same first step for those pipelines |

## The environment

`pyproject.toml` here is the workspace root, `uv.lock` holds every version, and `.python-version` holds the interpreter. Nothing is resolved at build time: a build installs what the lockfile says, so the versions under test are the versions that generate the pipeline.

```bash
uv sync --locked --all-packages   # both packages, with test dependencies
uv run pytest tests               # the generator's tests
```

Changing a dependency means editing that package's `pyproject.toml`, running `uv lock` here, and committing both.

## Pinning uv itself

Two scripts install uv: `bootstrap.sh` at the start of a build, and `ci_selector/recorders/fnrec/collect.sh` at the end of a recording one. They are separate Buildkite jobs on separate agents, and each is piped to bash with nothing on disk, so neither can share the other's. Both carry the version, the tarball and its checksum, and `tests/test_uv_pin.py` holds them equal. That test also checks that both GitHub workflows set up the same version, that the constants are the ones actually downloaded and checked, and that nothing names a second Python beside `.python-version`.

A uv already on the agent's `PATH` is used only if it reports the pinned version, so nothing is downloaded on a machine that already has the right one, and an agent cannot quietly decide the version for us. `VLLM_CI_UV_BIN` names a uv to run instead, for a rerun by hand or a test, and is taken at its word.

Three paths are still outside this. `bootstrap-intel.sh` installs the generator's dependencies with `pip` and no pins. `bootstrap-amd.sh` installs minijinja by piping an installer to a shell, at a pinned version but no checksum, and the AMD pipeline is a Jinja template rather than a Python one. `ci_selector/recorders/kernrec/collect.sh` runs its table builder under the agent's own `python3`. None of them is on the lockfile.
