<!-- ci-selector-shadow -->
### CI selector (shadow): 58 test steps (98 jobs) instead of 76 (109 jobs)

Shadow mode: this changes nothing about what CI runs. It shows what the evidence-based selector would pick for this PR, next to today's rules. [How it works](https://github.com/vllm-project/ci-infra/tree/main/buildkite/ci_selector).

**Feedback welcome:** reply here if it would skip a step this change needs, or runs something unrelated.

| steps (jobs) | Today's rules | Selector | Would skip | Would add |
|---|---|---|---|---|
| NVIDIA, CPU and others | 76 (109) | 58 (98) | 46 (57) | 28 (46) |
| AMD mirrors | 89 (139) | 35 (67) | 64 (92) | 10 (20) |

<details><summary>Selector would run (58)</summary>

- `amd-fp8-moe-kernels-mi355`
- `amd-kernels-mi355`
- `amd-lm-eval-small-models-harness`
- `amd-native-quantization-kernels-mi355`
- `arm-cpu-test` ×3
- `async-engine-inputs-utils-worker`
- `basic-models-test-other-cpu`
- `basic-models-tests-other`
- `batch-invariance-b200`
- `cpu-distributed-tests-dp-tp`
- `cpu-distributed-tests-pp-tp`
- `cpu-kernel-tests` ×2
- `cpu-language-generation-and-pooling-model-tests` ×3
- `cpu-multi-modal-model-tests-n` ×4
- `cpu-multimodal-config`
- `cpu-params-env-tokenizers-parser`
- `cpu-quantization-model-tests`
- `cpu-qwen2-5-vl-multimodal-tests`
- `cpu-reasoning-renderers`
- `cpu-spec-decode-tests`
- `deepseek-v4-kernel-test-b200`
- `deepseek-v4-kernel-test-h100`
- `distributed-dp-tests-4-gpus`
- `distributed-model-tests-2-gpus` ×3
- `engine`
- `entrypoints-integration-pooling`
- `fusion-and-compile-unit-tests-2xb200`
- `gemm-rs-ar-2xb200`
- `inkling-unit-tests-b200`
- `kernels-attention-test` ×7
- `kernels-b200` ×3
- `kernels-core-operation-test` ×3
- `kernels-deepgemm-test-h100`
- `kernels-flashmla-test-h100`
- `kernels-fusedmoe-layer-test-2-b200s`
- `kernels-fusedmoe-layer-test-2-h100s`
- `kernels-mhc-test-b200`
- `kernels-moe-test` ×5
- `kernels-quantization-test` ×6
- `kernels-root-misc-test-b200`
- `kimi-k3-unit-tests-b200`
- `model-executor`
- `multi-modal-models-standard-4-other-whisper`
- `multi-modal-processor-cpu` ×4
- `plugin-tests-2-gpus`
- `pytorch-compilation-passes-unit-tests`
- `pytorch-compilation-unit-tests`
- `quantization` ×4
- `quantized-models-test`
- `quantized-moe-test-b200`
- `qwen4-exp-unit-tests-cpu`
- `samplers-test`
- `v1-attention-b200` ×2
- `v1-attention-h100-mi300` ×2
- `v1-kv-connectors` ×4
- `v1-logits-oracle`
- `v1-others-cpu`
- `v1-spec-decode`
</details>

<details><summary>Would skip (today's rules run them) (46)</summary>

- `ascend-npu-test`
- `basic-correctness` ×2
- `basic-correctness-cpu-offload`
- `basic-correctness-cumem`
- `basic-correctness-prefetch-offload`
- `basic-correctness-sleep-mode`
- `basic-models-tests-initialization`
- `batch-invariance-h100`
- `benchmarks-cli-test`
- `distributed-compile-unit-tests-2xh100`
- `entrypoints-integration-api-server` ×4
- `entrypoints-integration-api-server-generate`
- `entrypoints-integration-api-server-openai-chat_completion`
- `entrypoints-integration-api-server-openai-completion`
- `entrypoints-integration-llm`
- `entrypoints-integration-multimodal`
- `entrypoints-integration-responses-api`
- `entrypoints-integration-speech_to_text`
- `fusion-e2e-quick-h100`
- `fusion-e2e-tp2-b200`
- `fusion-e2e-tp2-quick-h100`
- `kernels-fla-ops-test-b200`
- `language-models-tests-granite-l4-compatibility`
- `language-models-tests-hybrid` ×2
- `language-models-tests-standard`
- `lm-eval-dspark-watermark-2xh100`
- `lm-eval-small-models`
- `lm-eval-watermarking`
- `lora-tp-distributed` ×4
- `metrics-tracing-2-gpus`
- `multi-modal-models-standard-1-qwen2`
- `multi-modal-models-standard-2-qwen3-gemma`
- `multi-modal-models-standard-3-llava-qwen2-vl`
- `multi-modal-processor` ×4
- `pytorch-compilation-dynamic-shapes`
- `pytorch-compilation-unit-tests-h100`
- `pytorch-fullgraph-cudagraph-l4-compatibility`
- `pytorch-fullgraph-test`
- `quantized-fusions`
- `regression`
- `samplers-multimodal-beam-search`
- `v1-core`
- `v1-executor-worker`
- `v1-kv-offload`
- `v1-metrics-lmeval`
- `v1-sample`
</details>

<details><summary>Would add (today's rules do not run them) (28)</summary>

- `amd-kernels-mi355` (code map)
- `arm-cpu-test` ×3 (code map)
- `cpu-distributed-tests-dp-tp` (code map)
- `cpu-distributed-tests-pp-tp` (code map)
- `cpu-kernel-tests` ×2 (code map)
- `cpu-multi-modal-model-tests-n` ×4 (code map)
- `cpu-multimodal-config` (code map)
- `cpu-params-env-tokenizers-parser` (code map)
- `cpu-quantization-model-tests` (code map)
- `cpu-qwen2-5-vl-multimodal-tests` (code map)
- `cpu-reasoning-renderers` (code map)
- `cpu-spec-decode-tests` (code map)
- `deepseek-v4-kernel-test-b200` (code map)
- `deepseek-v4-kernel-test-h100` (code map)
- `distributed-dp-tests-4-gpus` (code map)
- `distributed-model-tests-2-gpus` ×3 (code map)
- `engine` (code map)
- `gemm-rs-ar-2xb200` (code map)
- `inkling-unit-tests-b200` (code map)
- `kernels-attention-test` ×7 (code map)
- `kernels-core-operation-test` ×3 (code map)
- `kernels-flashmla-test-h100` (code map)
- `kimi-k3-unit-tests-b200` (code map)
- `plugin-tests-2-gpus` (code map)
- `qwen4-exp-unit-tests-cpu` (code map)
- `v1-attention-b200` ×2 (code map)
- `v1-attention-h100-mi300` ×2 (code map)
- `v1-others-cpu` (code map)
</details>

<details><summary>AMD mirrors: would skip (64)</summary>

- `basic-correctness` ×2
- `basic-correctness-cpu-offload`
- `basic-correctness-cumem`
- `basic-correctness-prefetch-offload`
- `basic-correctness-sleep-mode`
- `basic-models-tests-extra-initialization` ×14
- `basic-models-tests-initialization`
- `basic-models-tests-other`
- `batch-invariance-h100`
- `benchmarks-cli-test`
- `deepseek-v4-kernel-test-b200`
- `entrypoints-integration-api-server` ×4
- `entrypoints-integration-api-server-generate`
- `entrypoints-integration-api-server-openai-chat_completion`
- `entrypoints-integration-api-server-openai-completion`
- `entrypoints-integration-llm`
- `entrypoints-integration-multimodal`
- `entrypoints-integration-responses-api`
- `entrypoints-integration-speech_to_text`
- `fusion-and-compile-unit-tests-2xb200`
- `fusion-e2e-quick-h100`
- `fusion-e2e-tp2-ar-rms-config-sweep-h100`
- `fusion-e2e-tp2-b200`
- `fusion-e2e-tp2-quick-h100`
- `kernels-fla-ops-test-b200`
- `language-models-tests-extra-standard` ×2
- `language-models-tests-granite-l4-compatibility`
- `language-models-tests-hybrid` ×2
- `language-models-tests-standard`
- `lm-eval-dspark-watermark-2xh100`
- `lm-eval-small-models`
- `lm-eval-turboquant-k3v4nc`
- `lm-eval-turboquant-k8v4`
- `lm-eval-turboquant-t3nc`
- `lm-eval-turboquant-t4nc`
- `lm-eval-watermarking`
- `lora-tp-distributed` ×4
- `metrics-tracing-2-gpus`
- `multi-modal-models-standard-1-qwen2`
- `multi-modal-models-standard-2-qwen3-gemma`
- `multi-modal-models-standard-3-llava-qwen2-vl`
- `multi-modal-processor` ×4
- `openai-api-correctness`
- `pipeline-context-parallelism-4-gpus`
- `platform-tests`
- `pytorch-compilation-dynamic-shapes`
- `pytorch-compilation-unit-tests-h100`
- `pytorch-fullgraph-test`
- `quantized-fusions`
- `regression`
- `samplers-multimodal-beam-search`
- `spec-decode-draft-model` ×4
- `spec-decode-eagle-1-deepseek-qwen`
- `spec-decode-eagle-2-llama3-qwen-vl-other`
- `spec-decode-mtp-deepseek-mimo`
- `spec-decode-mtp-gemma4`
- `spec-decode-mtp-qwen3-5`
- `spec-decode-ngram-suffix`
- `spec-decode-speculators`
- `v1-core`
- `v1-executor-worker`
- `v1-kv-offload`
- `v1-metrics-lmeval`
- `v1-sample`
</details>

<details><summary>AMD mirrors: would add (10)</summary>

- `deepseek-v4-kernel-test-h100` (code map)
- `engine` (code map)
- `gemm-rs-ar-2xb200` (code map)
- `kernels-attention-test` ×7 (code map)
- `kernels-core-operation-test` ×3 (code map)
- `kimi-k3-unit-tests-b200` (code map)
- `plugin-tests-2-gpus` (code map)
- `qwen4-exp-unit-tests` (code map)
- `v1-attention-b200` ×2 (code map)
- `v1-attention-h100-mi300` ×2 (code map)
</details>

#### CI results (2026-09-30 17:55 UTC)

215 passed, 5 failed, 5 pending.
CI is still running; the picture below is not final.

No misses: the selector would have run every failed job.

- `amd-mi300-moe-kernels-shard-2`: selector runs it
- `amd-mi300-moe-kernels-shard-5`: selector runs it
- `nvidia-h200-mig-35gb-v1-spec-decode`: selector runs it
- `nvidia-l4-moe-kernels-shard-2`: selector runs it
- `nvidia-l4-moe-kernels-shard-5`: selector runs it

<sub>33 changed files · base `d2fb35f66e` · head `cf934fc888` · Python record: not used (/tmp/ci-infra-selector/buildkite/ci_selector/coverage-data/table.json.gz is table version 5, expected 7; re-merge it from the raw recordings) · kernel record: table 866fa130fa (build 92059), map 866fa130fa · not counted: 10 build steps, 5 A100 steps the generator no longer emits, 21 optional steps the selector would also run</sub>
