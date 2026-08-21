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
  graph.sqlite.gz
  graph.sqlite.sha256
  manifest.json
test-selection/vllm/index.json
```

The manifest accounts for every discovered pytest job as healthy, missing, or
unhealthy. It binds both the deterministic gzip object and the logical SQLite
bytes; readers verify the compressed object before bounded decompression, then
verify the SQLite checksum sidecar. Missing and unhealthy jobs are never
omitted by the selector.

If publication fails after a nightly has collected its evidence, a trusted
main-nightly build can set `VLLM_CI_REPUBLISH_INVENTORY_B64` and
`VLLM_CI_REPUBLISH_SOURCE_BUILD` plus
`VLLM_CI_REPUBLISH_SOURCE_BUILD_ID` to enter the fail-closed recovery path.
The source build number is retained for human-readable provenance; the source
build UUID is required by `buildkite-agent artifact download --build`. The
generator validates the canonical inventory against the build commit and emits
exactly one CPU postmerge step; it does not render or rerun the fleet. Optional
`VLLM_CI_REPUBLISH_TRIALS_JSON` entries pin shadow trials to exact PR heads.
The recovery step downloads the source build's evidence, publishes with the
current ci-infra revision, and verifies a fresh read-back before any trial.
This path is intended only for trusted operators recovering immutable evidence,
not for normal nightly or pull-request execution.
Do not set any republish variable on a normal nightly: its presence deliberately
replaces the full generated fleet with the single recovery step.

Snapshot manifests are produced by this trusted publisher. Readers enforce the
publisher-declared decompression bound and both compressed and logical hashes;
they must not treat an untrusted manifest's size declaration as an independent
resource limit.

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
