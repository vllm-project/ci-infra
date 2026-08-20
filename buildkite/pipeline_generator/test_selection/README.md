# Trace-guided test selection

The vLLM main nightly automatically traces pytest jobs loaded from
`.buildkite/test_areas`. vLLM job YAML has no tracing fields.

Operator canaries must run on a non-main mirror branch. Set
`VLLM_CI_TRACE_CANARY_BRANCH` and `VLLM_CI_TRACE_CANARY_COMMIT` together; each
must exactly match the Buildkite branch/40-hex commit, `NIGHTLY=1` and a
non-production `VLLM_CI_TRACE_S3_PREFIX` are required, and pull requests are
rejected. The snapshot step renders a loud canary banner. For a bounded retry,
also set `VLLM_CI_ONLY_STEP_KEYS` to the exact failed/missing keys; the
generator keeps only their dependency closure and inventories only the traced
keys in that closure.

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

## Healthy-only retry merge

Automatic retry artifacts are attempt-scoped and carry
`BUILDKITE_RETRY_COUNT`; the materializer selects the highest attempt per shard
and fails closed on a corrupt or duplicate latest attempt. To accumulate a
separate targeted retry without editing an immutable snapshot, download the
base and retry builds into separate roots and run:

```bash
vllm-test-selection merge-fleet-graph \
  --base-input base --base-inventory base-inventory.json \
  --retry-input retry --retry-inventory retry-inventory.json \
  --base-source-build-id "$BASE_BUILDKITE_BUILD_ID" \
  --retry-source-build-id "$RETRY_BUILDKITE_BUILD_ID" \
  --merge-revision "$CI_INFRA_REVISION" \
  --output merged.sqlite --provenance-output merge-provenance.json
```

The command independently materializes both inputs against their own exact
collector hashes, permits retry policies only as an exact subset of the base,
overlays only retry jobs proven healthy, and verifies that every retained base
job and replacement job has byte-equivalent logical evidence. A mixed-source
graph records both collector hashes and leaves the legacy singular collector
field null. The provenance binds both source Buildkite UUIDs, both collector
and inventory hashes, the exact merger revision, and the merged graph hash.
Publish only after reviewing it, always to a new isolated prefix containing an
explicit `canary` path component (the recovery-only publisher rejects every
other prefix):

```bash
vllm-test-selection publish-graph \
  --graph merged.sqlite --bucket "$VLLM_CI_TRACE_S3_BUCKET" \
  --prefix test-selection/vllm/canary/<new-generation>
```

Never reuse either source prefix or the production prefix for a retry merge.

## Frozen #84714 image recovery

The non-main mirror branch resolves commit images from the premerge repository,
while #84714 ran on the postmerge repository. For the bounded #84714 recovery,
`VLLM_CI_RECOVERY_IMAGE_COPY=1` replaces the pipeline with one premerge CPU
step that carbon-copies the two hard-pinned #84714 postmerge manifests into the
mirror branch's premerge tags and verifies both public destination digests.
The mode accepts only the exact `ci-tsel-main-mirror-eac636a7` branch and
`eac636a7fa476983cdae34b45a984e9852aad375` commit with their paired trace
canary authorization. It rejects PRs, other repositories or registries,
`VLLM_CI_ONLY_STEP_KEYS`, and every republish variable.

This is a frozen recovery vehicle, not a general image-copy API. Run it only
after any build writing the same premerge tags is terminal. The subsequent
instrumented retry must verify both destination digests and require its normal
image steps to log `Image already exists` / `Skipping build`.

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
