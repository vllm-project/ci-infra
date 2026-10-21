<!-- ci-selector-shadow -->
### CI selector (shadow): 31 test steps (67 jobs) instead of 58 (90 jobs)

Shadow mode: this changes nothing about what CI runs. It shows what the evidence-based selector would pick for this PR, next to today's rules. [How it works](https://github.com/vllm-project/ci-infra/tree/main/buildkite/ci_selector).

**Feedback welcome:** reply here if it would skip a step this change needs, or runs something unrelated.

| steps (jobs) | Today's rules | Selector | Would skip | Would add |
|---|---|---|---|---|
| NVIDIA, CPU and others | 58 (90) | 31 (67) | 38 (42) | 11 (19) |
| AMD mirrors | 58 (88) | 26 (55) | 39 (43) | 7 (10) |

<details><summary>Selector would run (31)</summary>

- `arm-cpu-test` ×3
- `basic-models-tests-extra-initialization` ×14
- `basic-models-tests-initialization`
- `basic-models-tests-other`
- `cpu-language-generation-and-pooling-model-tests` ×3
- `cpu-multi-modal-model-tests-n` ×4
- `cpu-params-env-tokenizers-parser`
- `cpu-reasoning-renderers`
- `distributed-model-tests-2-gpus` ×3
- `engine`
- `entrypoints-integration-api-server` ×4
- `entrypoints-integration-pooling`
- `entrypoints-integration-speech_to_text`
- `entrypoints-unit-tests`
- `examples`
- `kernels-root-misc-test-b200`
- `language-models-tests-extra-standard` ×2
- `language-models-tests-granite-l4-compatibility`
- `language-models-tests-hybrid` ×2
- `language-models-tests-standard`
- `model-executor`
- `multi-modal-models-standard-1-qwen2`
- `multi-modal-models-standard-4-other-whisper`
- `multi-modal-processor` ×4
- `multi-modal-processor-cpu` ×4
- `pipeline-context-parallelism-4-gpus`
- `plugin-tests-2-gpus`
- `pytorch-compilation-unit-tests`
- `quantization` ×4
- `rayexecutorv2-4-gpus`
- `rust-frontend-serve-admin-coverage`
</details>

<details><summary>Would skip (today's rules run them) (38)</summary>

- `amd-lm-eval-small-models-harness`
- `ascend-npu-test`
- `async-engine-inputs-utils-worker`
- `basic-correctness` ×2
- `basic-correctness-cpu-offload`
- `basic-correctness-cumem`
- `basic-correctness-prefetch-offload`
- `basic-correctness-sleep-mode`
- `basic-models-test-other-cpu`
- `benchmarks-cli-test`
- `entrypoints-integration-api-server-generate`
- `entrypoints-integration-api-server-openai-chat_completion`
- `entrypoints-integration-api-server-openai-completion`
- `entrypoints-integration-llm`
- `entrypoints-integration-multimodal`
- `entrypoints-integration-responses-api`
- `fusion-e2e-quick-h100`
- `fusion-e2e-tp2-b200`
- `fusion-e2e-tp2-quick-h100`
- `kernels-fla-ops-test-b200`
- `kernels-mhc-test-b200`
- `metrics-tracing-2-gpus`
- `multi-modal-models-standard-2-qwen3-gemma`
- `multi-modal-models-standard-3-llava-qwen2-vl`
- `pytorch-compilation-dynamic-shapes`
- `pytorch-compilation-passes-unit-tests`
- `pytorch-compilation-unit-tests-h100`
- `pytorch-fullgraph-cudagraph-l4-compatibility`
- `pytorch-fullgraph-test`
- `regression`
- `v1-core`
- `v1-executor-worker`
- `v1-kv-connectors` ×4
- `v1-kv-offload`
- `v1-logits-oracle`
- `v1-metrics-lmeval`
- `v1-sample`
- `v1-spec-decode`
</details>

<details><summary>Would add (today's rules do not run them) (11)</summary>

- `arm-cpu-test` ×3 (code map)
- `cpu-multi-modal-model-tests-n` ×4 (code map)
- `cpu-params-env-tokenizers-parser` (code map)
- `cpu-reasoning-renderers` (code map)
- `engine` (code map)
- `entrypoints-unit-tests` (code map)
- `examples` (code map)
- `plugin-tests-2-gpus` (code map)
- `quantization` ×4 (code map)
- `rayexecutorv2-4-gpus` (code map)
- `rust-frontend-serve-admin-coverage` (code map)
</details>

<details><summary>AMD mirrors: would skip (39)</summary>

- `async-engine-inputs-utils-worker`
- `basic-correctness` ×2
- `basic-correctness-cpu-offload`
- `basic-correctness-cumem`
- `basic-correctness-prefetch-offload`
- `basic-correctness-sleep-mode`
- `basic-models-test-other-cpu`
- `benchmarks-cli-test`
- `entrypoints-integration-api-server-generate`
- `entrypoints-integration-api-server-openai-chat_completion`
- `entrypoints-integration-api-server-openai-completion`
- `entrypoints-integration-llm`
- `entrypoints-integration-multimodal`
- `entrypoints-integration-responses-api`
- `fusion-e2e-quick-h100`
- `fusion-e2e-tp2-ar-rms-config-sweep-h100`
- `fusion-e2e-tp2-b200`
- `fusion-e2e-tp2-quick-h100`
- `kernels-fla-ops-test-b200`
- `kernels-mhc-test-b200`
- `lm-eval-small-models`
- `metrics-tracing-2-gpus`
- `mrcr-eval-small-models`
- `multi-modal-models-standard-2-qwen3-gemma`
- `multi-modal-models-standard-3-llava-qwen2-vl`
- `platform-tests`
- `pytorch-compilation-dynamic-shapes`
- `pytorch-compilation-passes-unit-tests`
- `pytorch-compilation-unit-tests-h100`
- `pytorch-fullgraph-test`
- `regression`
- `v1-core`
- `v1-executor-worker`
- `v1-kv-connectors` ×4
- `v1-kv-offload`
- `v1-logits-oracle`
- `v1-metrics-lmeval`
- `v1-sample`
- `v1-spec-decode`
</details>

<details><summary>AMD mirrors: would add (7)</summary>

- `engine` (code map)
- `entrypoints-unit-tests` (code map)
- `examples` (code map)
- `fusion-e2e-tp2-asynctp-config-sweep-h100` (code map)
- `plugin-tests-2-gpus` (code map)
- `quantization` ×4 (code map)
- `rust-frontend-serve-admin-coverage` (code map)
</details>

#### CI results (2026-09-30 06:22 UTC)

144 passed, 1 failed, 0 pending.

No misses: the selector would have run every failed job.

- `nvidia-h200-mig-35gb-entrypoints-integration-po`: selector runs it

<sub>2 changed files · base `e006d761a5` · head `f4c9c45324` · Python record: not used (/tmp/ci-infra-selector/buildkite/ci_selector/coverage-data/table.json.gz is table version 5, expected 7; re-merge it from the raw recordings) · kernel record: table d882bddbea (build 91957), map d882bddbea · not counted: 10 build steps, 5 A100 steps the generator no longer emits, 16 optional steps the selector would also run</sub>
