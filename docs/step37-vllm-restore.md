# Step 37 — restore GitHub config and schedules in the vllm org

Derived 2026-09-04 by diffing the pre-freeze tpu-commons snapshot
(`/tmp/tpu_commons_pipeline_snapshot.json`, captured before any freeze edits)
against live vllm config. Raw data: `/tmp/org_config_compare.json`.

Every schedule already exists in vllm with identical `cronline`, `branch`, `env`,
`commit` and `message`. **Nothing needs to be created — only enabled.**

## 1. `trigger_mode: code` — 6 pipelines

    tpu-inference-benchmark
    tpu-inference-ci
    tpu-inference-disagg-gke-benchmark
    tpu-inference-kernel-tuning
    tpu-vllm-integration
    vllm-torchtpu-ci

The other 6 TPU pipelines were already `none` in tpu-commons before the freeze and
must stay `none`: tpu-inference-dev, vllm-torchtpu-dev, vllm-torchtpu-image,
vllm-torchtpu-integration, vllm-torchtpu-nightly-image, vllm-torchtpu-pd-disagg-pipeline.

## 2. `publish_commit_status: true` — 10 pipelines

    tpu-inference-benchmark              tpu-vllm-integration
    tpu-inference-ci                     vllm-torchtpu-ci
    tpu-inference-dev                    vllm-torchtpu-dev
    tpu-inference-disagg-gke-benchmark   vllm-torchtpu-integration
    tpu-inference-kernel-tuning          vllm-torchtpu-nightly-image

Leave `false` on vllm-torchtpu-image and vllm-torchtpu-pd-disagg-pipeline — they
were `false` in tpu-commons too.

`publish_commit_status_per_step` already matches in both orgs (true on
tpu-inference-ci and vllm-torchtpu-ci only). No change needed.
`build_pull_requests`, `build_branches`, and the other `build_*` flags already match.

## 3. Enable 9 schedules

Two are already enabled in vllm (tpu-inference-ci "nightly", and
vllm-torchtpu-nightly-image "Nightly"), so 9 of the 11 need flipping:

| pipeline | schedule | cronline |
|---|---|---|
| tpu-inference-benchmark | Hourly Benchmark | `15 * * * * America/Los_Angeles` |
| tpu-inference-benchmark | Daily Benchmark | `30 4 * * * America/Los_Angeles` |
| tpu-inference-ci | nightly with MODEL_IMPL_TYPE=flax_nnx | `0 3 * * * America/Los_Angeles` |
| tpu-inference-ci | nightly with MODEL_IMPL_TYPE=vllm | `30 1 * * * America/Los_Angeles` |
| tpu-inference-disagg-gke-benchmark | Daily Script Run | `0 0 * * *` |
| tpu-inference-kernel-tuning | Weekly Kernel E2E Autotune | `0 0 * * 0` |
| tpu-vllm-integration | :hourglass: Hourly vLLM Integration Build | `0 */6 * * *` |
| vllm-torchtpu-ci | Nighly | `0 2 * * * America/Los_Angeles` |
| vllm-torchtpu-pd-disagg-pipeline | nightly | `30 6 * * *` |

Note the tpu-commons `Weekly Kernel E2E Autotune` was already disabled before the
freeze, so enabling it in vllm is a behaviour change — confirm before flipping.

## 4. Open item

`kube-dev` has no counterpart pipeline in vllm. It was `trigger_mode: code` with
`publish_commit_status: true` and no schedules. Migrate or retire — still undecided.
