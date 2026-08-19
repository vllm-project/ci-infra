# Trace-guided test selection

The vLLM main nightly automatically traces pytest jobs loaded from
`.buildkite/test_areas`. vLLM job YAML has no tracing fields.

## Evidence

Each pytest job records presence-only edges:

- Python: repository file and line → pytest node → Buildkite job.
- NVIDIA: mangled CUDA kernel identity → pytest node/job.

Counts, call order, Python stacks, and raw Nsight timelines are not stored in
the selection database. Unsupported or unhealthy jobs remain always-run.

The generator publishes one deterministic collector bundle. A test job polls
briefly for that bundle and otherwise runs its original command script
unchanged. Collector failure is evidence loss, not a test result.

## Snapshot

The independent fan-in waits for the traced jobs, downloads their compact
artifacts, and builds `graph.sqlite`. It publishes immutable objects under:

```text
test-selection/vllm/snapshots/<main-sha>/
  graph.sqlite
  graph.sqlite.sha256
  manifest.json
test-selection/vllm/index.json
```

The manifest accounts for every discovered pytest job as healthy, missing, or
unhealthy. Missing and unhealthy jobs are never omitted by the selector.

## PR selection

The PR path fetches the newest fresh snapshot whose commit is an ancestor of
the PR merge base, verifies all checksums, and joins changed files to jobs.

```bash
vllm-test-selection fetch-snapshot \
  --bucket "$VLLM_CI_TRACE_S3_BUCKET" \
  --repo /path/to/vllm --base "$MERGE_BASE" --output graph.sqlite

vllm-test-selection current-jobs \
  --pipeline pipeline.yaml --output current-jobs.json

vllm-test-selection select \
  --graph graph.sqlite --repo /path/to/vllm \
  --base "$MERGE_BASE" --head "$PR_HEAD" \
  --current-jobs current-jobs.json
```

Any stale/non-ancestral snapshot, checksum error, unmapped changed file, or
invalid input returns fallback status. The caller must then leave
`VLLM_CI_ONLY_STEP_KEYS` unset and run the normal pipeline. Selection remains
shadow-only until fleet evidence and false-negative audits justify enforcement.
