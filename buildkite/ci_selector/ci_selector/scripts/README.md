# scripts

Offline tools that build the coverage table. They are not on the selection path
and never run on a PR: `ci-select` reads the finished table off disk.

Both need the package installed (`uv sync` from `buildkite/ci_selector`).

## 1. Download a build

```bash
export BK_TOKEN=...      # Buildkite token with read_builds + read_artifacts

ci-fetch-build https://buildkite.com/<org>/<pipeline>/builds/<n> --out sweeps/
```

Writes `sweeps/<org>-<pipeline>-<n>/`. Safe to re-run: finished jobs are
skipped, so an interrupted download resumes.

| flag | |
| --- | --- |
| `--out DIR` | required, where sweeps are written |
| `--org` / `--pipeline` | needed only when you pass a build number instead of a URL |
| `--limit N` | stop after N jobs, for a smoke test |
| `--force` | refetch jobs already on disk |
| `--allow-partial` | report a low recording rate instead of exiting 2 |

Exits 2 if fewer than half the started jobs delivered a recording, which means
the build failed to deliver rather than ran no code.

## 2. Build the table

```bash
ci-build-table <vllm-repo> sweeps/<org>-<pipeline>-<n> -o table.json.gz
```

Takes one or more sweep directories and merges them. The repo argument must
contain the commit the build ran at, or the merge aborts.

| flag | |
| --- | --- |
| `-o FILE` | required, the table to write |
| `-v` | per-build progress |
| `--allow-partial` | merge even from a build that delivered almost nothing |

## 3. Use it

```bash
ci-select --repo /path/to/vllm --diff <base>...<head> --table table.json.gz
```

`CI_SELECTOR_TABLE` sets the same thing as an environment variable.

## 4. The kernel record

Not built here: the recording build publishes it (`kernrec/collect.sh`).
This only fetches it.

```bash
ci-fetch-kernel-record                      # follows <bucket>/ci/latest.json
ci-fetch-kernel-record --commit <sha>       # one published commit
```

Writes `kernel_table.json.gz` and `kernel_symbol_map.json.gz` into
`coverage-data/`, after validating both and checking they were recorded at the
same commit. Public bucket, plain HTTPS, no token. Exit 1 leaves whatever was
on disk untouched.
