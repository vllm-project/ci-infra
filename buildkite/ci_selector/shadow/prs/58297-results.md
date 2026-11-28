<!-- ci-selector-shadow -->
### CI selector (shadow): 27 test steps (49 jobs) instead of 72 (96 jobs)

Shadow mode: this changes nothing about what CI runs. It shows what the evidence-based selector would pick for this PR, next to today's rules. [How it works](https://github.com/vllm-project/ci-infra/tree/main/buildkite/ci_selector).

**Feedback welcome:** reply here if it would skip a step this change needs, or runs something unrelated.

| steps (jobs) | Today's rules | Selector | Would skip | Would add |
|---|---|---|---|---|
| NVIDIA, CPU and others | 72 (96) | 27 (49) | 57 (67) | 12 (20) |
| AMD mirrors | 76 (103) | 11 (25) | 69 (85) | 4 (7) |

<details><summary>Selector would run (27)</summary>

- `amd-kernels-mi355`
- `arm-cpu-test` ×3
- `basic-models-test-other-cpu`
- `cpu-kernel-tests` ×2
- `cpu-multimodal-config`
- `cpu-params-env-tokenizers-parser`
- `cpu-quantization-model-tests`
- `cpu-reasoning-renderers`
- `cpu-tool-parsers`
- `docker-build-metadata`
- `engine-1-gpu`
- `entrypoints-integration-api-server` ×4
- `kernels-attention-diffkv-test-h100`
- `kernels-attention-test` ×7
- `kernels-b200` ×3
- `kernels-flashmla-test-h100`
- `kernels-root-misc-test-b200`
- `multi-modal-processor-cpu` ×4
- `pytorch-compilation-passes-unit-tests`
- `pytorch-compilation-unit-tests`
- `quantization` ×4
- `quantized-models-test`
- `qwen4-exp-unit-tests-cpu`
- `v1-attention-b200` ×2
- `v1-attention-h100-mi300` ×2
- `v1-logits-oracle`
- `v1-others-cpu`
</details>

<details><summary>Would skip (today's rules run them) (57)</summary>

- `amd-lm-eval-small-models-harness`
- `ascend-npu-test`
- `async-engine-inputs-utils-worker`
- `basic-correctness` ×2
- `basic-correctness-cpu-offload`
- `basic-correctness-cumem`
- `basic-correctness-prefetch-offload`
- `basic-correctness-sleep-mode`
- `basic-models-tests-initialization`
- `basic-models-tests-other`
- `batch-invariance-b200`
- `batch-invariance-h100`
- `benchmarks-cli-test`
- `cpu-language-generation-and-pooling-model-tests` ×3
- `e2e-core-1-gpu`
- `e2e-core-large-memory`
- `e2e-scheduling-1-gpu`
- `engine`
- `entrypoints-integration-api-server-generate`
- `entrypoints-integration-api-server-openai-chat_completion`
- `entrypoints-integration-api-server-openai-completion`
- `entrypoints-integration-llm`
- `entrypoints-integration-multimodal`
- `entrypoints-integration-pooling`
- `entrypoints-integration-responses-api`
- `entrypoints-integration-speech_to_text`
- `fusion-e2e-quick-h100`
- `fusion-e2e-tp2-b200`
- `fusion-e2e-tp2-quick-h100`
- `kernels-fla-ops-test-b200`
- `kernels-mhc-test-b200`
- `language-models-tests-granite-l4-compatibility`
- `language-models-tests-hybrid` ×2
- `language-models-tests-standard`
- `metrics-tracing-2-gpus`
- `multi-modal-models-standard-1-qwen2`
- `multi-modal-models-standard-2-qwen3-gemma`
- `multi-modal-models-standard-3-llava-qwen2-vl`
- `multi-modal-models-standard-4-other-whisper`
- `multi-modal-processor` ×4
- `platform-tests`
- `pytorch-compilation-dynamic-shapes`
- `pytorch-compilation-unit-tests-h100`
- `pytorch-fullgraph-cudagraph-l4-compatibility`
- `pytorch-fullgraph-test`
- `regression`
- `spec-decode-mtp-deepseek-mimo`
- `spec-decode-mtp-gemma4`
- `spec-decode-mtp-qwen3-5`
- `spec-decode-speculators`
- `v1-core`
- `v1-executor-worker`
- `v1-kv-connectors` ×4
- `v1-kv-offload`
- `v1-metrics-lmeval`
- `v1-sample`
- `v1-spec-decode`
</details>

<details><summary>Would add (today's rules do not run them) (12)</summary>

- `amd-kernels-mi355` (code map)
- `arm-cpu-test` ×3 (code map)
- `cpu-kernel-tests` ×2 (code map)
- `cpu-quantization-model-tests` (code map)
- `docker-build-metadata` (code map)
- `engine-1-gpu` (code map)
- `kernels-attention-diffkv-test-h100` (code map)
- `kernels-b200` ×3 (code map)
- `kernels-flashmla-test-h100` (code map)
- `quantization` ×4 (code map)
- `quantized-models-test` (code map)
- `qwen4-exp-unit-tests-cpu` (code map)
</details>

<details><summary>AMD mirrors: would skip (69)</summary>

- `async-engine-inputs-utils-worker`
- `basic-correctness` ×2
- `basic-correctness-cpu-offload`
- `basic-correctness-cumem`
- `basic-correctness-prefetch-offload`
- `basic-correctness-sleep-mode`
- `basic-models-test-other-cpu`
- `basic-models-tests-initialization`
- `basic-models-tests-other`
- `batch-invariance-h100`
- `benchmarks-cli-test`
- `distributed-model-tests-2-gpus` ×3
- `e2e-core-1-gpu`
- `e2e-core-large-memory`
- `e2e-scheduling-1-gpu`
- `engine`
- `entrypoints-integration-api-server-generate`
- `entrypoints-integration-api-server-openai-chat_completion`
- `entrypoints-integration-api-server-openai-completion`
- `entrypoints-integration-llm`
- `entrypoints-integration-multimodal`
- `entrypoints-integration-pooling`
- `entrypoints-integration-responses-api`
- `entrypoints-integration-speech_to_text`
- `extract-hidden-states-integration-2-gpus`
- `fault-tolerance-e2e-2xh100`
- `fusion-and-compile-unit-tests-2xb200`
- `fusion-e2e-quick-h100`
- `fusion-e2e-tp2-ar-rms-config-sweep-h100`
- `fusion-e2e-tp2-b200`
- `fusion-e2e-tp2-quick-h100`
- `kernels-b200` ×3
- `kernels-fla-ops-test-b200`
- `kernels-fusedmoe-layer-test-2-b200s`
- `kernels-fusedmoe-layer-test-2-h100s`
- `kernels-mhc-test-b200`
- `kernels-root-misc-test-b200`
- `language-models-tests-extra-standard` ×2
- `language-models-tests-granite-l4-compatibility`
- `language-models-tests-hybrid` ×2
- `language-models-tests-standard`
- `lm-eval-small-models`
- `metrics-tracing-2-gpus`
- `mrcr-eval-small-models`
- `multi-modal-models-standard-1-qwen2`
- `multi-modal-models-standard-2-qwen3-gemma`
- `multi-modal-models-standard-3-llava-qwen2-vl`
- `multi-modal-models-standard-4-other-whisper`
- `multi-modal-processor` ×4
- `multi-modal-processor-cpu` ×4
- `openai-api-correctness`
- `pipeline-context-parallelism-4-gpus`
- `platform-tests`
- `pytorch-compilation-dynamic-shapes`
- `pytorch-compilation-unit-tests-h100`
- `pytorch-fullgraph-test`
- `quantized-moe-test-b200`
- `regression`
- `spec-decode-mtp-deepseek-mimo`
- `spec-decode-mtp-gemma4`
- `spec-decode-mtp-qwen3-5`
- `spec-decode-speculators`
- `v1-core`
- `v1-executor-worker`
- `v1-kv-connectors` ×4
- `v1-kv-offload`
- `v1-metrics-lmeval`
- `v1-sample`
- `v1-spec-decode`
</details>

<details><summary>AMD mirrors: would add (4)</summary>

- `engine-1-gpu` (code map)
- `kernels-attention-diffkv-test-h100` (code map)
- `quantization` ×4 (code map)
- `quantized-models-test` (code map)
</details>

#### CI results (2026-09-30 06:52 UTC)

0 passed, 0 failed, 0 pending.

No failures to judge.

<sub>7 changed files · base `90e13fc757` · head `3034547690` · Python record: not used (/tmp/ci-infra-selector/buildkite/ci_selector/coverage-data/table.json.gz is table version 5, expected 7; re-merge it from the raw recordings) · kernel record: table d882bddbea (build 91957), map d882bddbea · not counted: 10 build steps, 5 A100 steps the generator no longer emits, 8 optional steps the selector would also run</sub>
