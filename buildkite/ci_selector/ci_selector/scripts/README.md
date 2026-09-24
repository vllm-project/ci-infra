# scripts

Tools for the two coverage records: building one, publishing it, and fetching
a published one. None is on the selection path and none runs on a PR.
`ci-select` reads the finished records off disk.

They all need the package installed (`uv sync` from `buildkite/ci_selector`).

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

The collect step calls the same builder on a build's downloaded artifacts,
where there is no Buildkite token and so no sweep:

```bash
ci-build-table <vllm-repo> --fnrec .fnrec \
  --build 42 --commit <sha> --expected-jobs 30 -o table.json.gz
```

| flag | |
| --- | --- |
| `--fnrec DIR` | fold this artifact tree instead of a sweep |
| `--build` / `--commit` | required with `--fnrec`; a sweep carries them |
| `--pipeline` | defaults to `ci` |
| `--expected-jobs N` | how many jobs were armed, for the delivery check |

A table is refused when fewer than half the expected jobs delivered. That is a
delivery failure, not a build that ran no vLLM code.

**Tables are versioned.** A table built before the version last changed loads
as unavailable and has to be re-merged from its sweep; the selector runs on
the code map alone until it is.

## 3. Use it

```bash
ci-select --repo /path/to/vllm --diff <base>...<head> --table table.json.gz
```

`CI_SELECTOR_TABLE` sets the same thing as an environment variable.

## 4. Publish the function record

```bash
ci-publish-function-record table.json.gz    # --dry-run to see the target first
```

Uploads it to `<bucket>/<pipeline>/fnrec/<commit>/`, then moves that prefix's
`latest.json`, so a reader following the pointer never finds a half-written
table. Refuses a table that records no commit, or more than one, since neither
has a prefix to publish under. Needs `aws` and write credentials.

## 5. Fetch a published record

```bash
ci-fetch-function-record                 # follows <bucket>/ci/fnrec/latest.json
ci-fetch-kernel-record                   # and <bucket>/ci/kernrec/latest.json
ci-fetch-function-record --commit <sha>  # one published commit
```

The first writes `table.json.gz` into `coverage-data/`; the second writes
`kernel_table.json.gz` and `kernel_symbol_map.json.gz`, after checking both
were recorded at the same commit. Both validate what they downloaded before it
replaces what is on disk. Public bucket, plain HTTPS, no token. Exit 1 leaves
whatever was there untouched.

The kernel record is not built here: its recording build folds and publishes
itself (`recorders/kernrec/collect.sh`).
