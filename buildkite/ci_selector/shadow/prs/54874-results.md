<!-- ci-selector-shadow -->
### CI selector (shadow): 61 test steps (112 jobs) instead of 57 (75 jobs)

Shadow mode: this changes nothing about what CI runs. It shows what the evidence-based selector would pick for this PR, next to today's rules. [How it works](https://github.com/vllm-project/ci-infra/tree/main/buildkite/ci_selector).

**Feedback welcome:** reply here if it would skip a step this change needs, or runs something unrelated.

| steps (jobs) | Today's rules | Selector | Would skip | Would add |
|---|---|---|---|---|
| NVIDIA, CPU and others | 57 (75) | 61 (112) | 31 (36) | 35 (73) |
| AMD mirrors | 50 (68) | 46 (92) | 27 (32) | 23 (56) |

<details><summary>Selector would run (61)</summary>

- `amd-fp4-moe-kernels-mi355`
- `amd-fp8-moe-kernels-mi355`
- `arm-cpu-test` ×3
- `async-engine-inputs-utils-worker`
- `basic-models-test-other-cpu`
- `basic-models-tests-other`
- `batch-invariance-b200`
- `cpu-kernel-tests` ×2
- `cpu-language-generation-and-pooling-model-tests` ×3
- `cpu-params-env-tokenizers-parser`
- `cpu-spec-decode-tests`
- `cudagraph`
- `deepseek-v4-kernel-test-b200`
- `deepseek-v4-kernel-test-h100`
- `distributed-compile-unit-tests-2xh100`
- `distributed-dp-tests-4-gpus`
- `distributed-model-tests-2-gpus` ×3
- `e2e-core-1-gpu`
- `e2e-core-large-memory`
- `engine-1-gpu`
- `entrypoints-integration-multimodal`
- `fusion-and-compile-unit-tests-2xb200`
- `kernels-attention-diffkv-test-h100`
- `kernels-attention-test` ×7
- `kernels-b200` ×3
- `kernels-core-operation-test` ×3
- `kernels-fla-ops-test-b200`
- `kernels-fusedmoe-layer-test-2-b200s`
- `kernels-fusedmoe-layer-test-2-h100s`
- `kernels-helion-test` ×5
- `kernels-mamba-test`
- `kernels-mhc-test-b200`
- `kernels-minimax-reduce-rms-test-2-gpus`
- `kernels-moe-test` ×5
- `kernels-quantization-test` ×6
- `kernels-root-misc-test-b200`
- `language-models-tests-extra-standard` ×2
- `language-models-tests-standard`
- `lora` ×4
- `model-executor`
- `multi-modal-models-standard-4-other-whisper`
- `multi-modal-processor` ×4
- `multi-modal-processor-cpu` ×4
- `pytorch-compilation-dynamic-shapes`
- `pytorch-compilation-passes-unit-tests`
- `pytorch-compilation-unit-tests`
- `pytorch-compilation-unit-tests-h100`
- `pytorch-fullgraph-cudagraph-l4-compatibility`
- `pytorch-fullgraph-test`
- `quantization` ×4
- `quantized-models-test`
- `qwen4-exp-unit-tests-cpu`
- `spec-decode-draft-model` ×4
- `v1-attention-b200` ×2
- `v1-attention-h100-mi300` ×2
- `v1-executor-worker`
- `v1-kv-connectors` ×4
- `v1-kv-offload`
- `v1-others-cpu`
- `v1-sample`
- `v1-spec-decode`
</details>

<details><summary>Would skip (today's rules run them) (31)</summary>

- `ascend-npu-test`
- `basic-correctness` ×2
- `basic-correctness-cpu-offload`
- `basic-correctness-cumem`
- `basic-correctness-prefetch-offload`
- `basic-correctness-sleep-mode`
- `basic-models-tests-initialization`
- `benchmarks-cli-test`
- `cpu-multimodal-config`
- `cpu-reasoning-renderers`
- `cpu-tool-parsers`
- `engine`
- `entrypoints-integration-api-server` ×4
- `entrypoints-integration-api-server-generate`
- `entrypoints-integration-api-server-openai-chat_completion`
- `entrypoints-integration-api-server-openai-completion`
- `entrypoints-integration-llm`
- `entrypoints-integration-pooling`
- `entrypoints-integration-responses-api`
- `entrypoints-integration-speech_to_text`
- `language-models-tests-granite-l4-compatibility`
- `language-models-tests-hybrid` ×2
- `metrics-tracing-2-gpus`
- `multi-modal-models-standard-1-qwen2`
- `multi-modal-models-standard-2-qwen3-gemma`
- `multi-modal-models-standard-3-llava-qwen2-vl`
- `platform-tests`
- `regression`
- `v1-core`
- `v1-logits-oracle`
- `v1-metrics-lmeval`
</details>

<details><summary>Would add (today's rules do not run them) (35)</summary>

- `amd-fp4-moe-kernels-mi355` (code map)
- `amd-fp8-moe-kernels-mi355` (code map)
- `arm-cpu-test` ×3 (code map)
- `batch-invariance-b200` (code map)
- `cpu-kernel-tests` ×2 (code map)
- `cpu-spec-decode-tests` (code map)
- `cudagraph` (code map)
- `deepseek-v4-kernel-test-b200` (code map)
- `deepseek-v4-kernel-test-h100` (code map)
- `distributed-compile-unit-tests-2xh100` (code map)
- `distributed-dp-tests-4-gpus` (code map)
- `distributed-model-tests-2-gpus` ×3 (code map)
- `e2e-core-1-gpu` (code map)
- `e2e-core-large-memory` (code map)
- `engine-1-gpu` (code map)
- `fusion-and-compile-unit-tests-2xb200` (code map)
- `kernels-attention-diffkv-test-h100` (code map)
- `kernels-attention-test` ×7 (code map)
- `kernels-b200` ×3 (code map)
- `kernels-fusedmoe-layer-test-2-b200s` (code map)
- `kernels-fusedmoe-layer-test-2-h100s` (code map)
- `kernels-helion-test` ×5 (code map)
- `kernels-mamba-test` (code map)
- `kernels-minimax-reduce-rms-test-2-gpus` (code map)
- `kernels-moe-test` ×5 (code map)
- `kernels-quantization-test` ×6 (code map)
- `language-models-tests-extra-standard` ×2 (code map)
- `lora` ×4 (code map)
- `model-executor` (code map)
- `quantization` ×4 (code map)
- `quantized-models-test` (code map)
- `qwen4-exp-unit-tests-cpu` (code map)
- `spec-decode-draft-model` ×4 (code map)
- `v1-attention-b200` ×2 (code map)
- `v1-attention-h100-mi300` ×2 (code map)
</details>

<details><summary>AMD mirrors: would skip (27)</summary>

- `basic-correctness` ×2
- `basic-correctness-cpu-offload`
- `basic-correctness-cumem`
- `basic-correctness-prefetch-offload`
- `basic-correctness-sleep-mode`
- `basic-models-tests-initialization`
- `benchmarks-cli-test`
- `engine`
- `entrypoints-integration-api-server` ×4
- `entrypoints-integration-api-server-generate`
- `entrypoints-integration-api-server-openai-chat_completion`
- `entrypoints-integration-api-server-openai-completion`
- `entrypoints-integration-llm`
- `entrypoints-integration-pooling`
- `entrypoints-integration-responses-api`
- `entrypoints-integration-speech_to_text`
- `language-models-tests-granite-l4-compatibility`
- `language-models-tests-hybrid` ×2
- `metrics-tracing-2-gpus`
- `multi-modal-models-standard-1-qwen2`
- `multi-modal-models-standard-2-qwen3-gemma`
- `multi-modal-models-standard-3-llava-qwen2-vl`
- `platform-tests`
- `regression`
- `v1-core`
- `v1-logits-oracle`
- `v1-metrics-lmeval`
</details>

<details><summary>AMD mirrors: would add (23)</summary>

- `cudagraph` (code map)
- `deepseek-v4-kernel-test-b200` (code map)
- `distributed-model-tests-2-gpus` ×3 (code map)
- `e2e-core-1-gpu` (code map)
- `e2e-core-large-memory` (code map)
- `engine-1-gpu` (code map)
- `kernels-attention-diffkv-test-h100` (code map)
- `kernels-attention-test` ×7 (code map)
- `kernels-flashmla-test-h100` (code map)
- `kernels-helion-test` ×5 (code map)
- `kernels-mamba-test` (code map)
- `kernels-minimax-reduce-rms-test-2-gpus` (code map)
- `kernels-moe-test` ×5 (code map)
- `kernels-quantization-test` ×6 (code map)
- `language-models-tests-extra-standard` ×2 (code map)
- `lora` ×4 (code map)
- `model-executor` (code map)
- `quantization` ×4 (code map)
- `quantized-models-test` (code map)
- `qwen4-exp-unit-tests` (code map)
- `spec-decode-draft-model` ×4 (code map)
- `v1-attention-b200` ×2 (code map)
- `v1-attention-h100-mi300` ×2 (code map)
</details>

#### CI results (2026-09-30 05:36 UTC)

124 passed, 0 failed, 0 pending.

No failures to judge.

<sub>3 changed files · base `70ae0b7435` · head `7230dfea50` · Python record: not used (/tmp/ci-infra-selector/buildkite/ci_selector/coverage-data/table.json.gz is table version 5, expected 7; re-merge it from the raw recordings) · kernel record: table d882bddbea (build 91957), map d882bddbea · not counted: 10 build steps, 5 A100 steps the generator no longer emits, 20 optional steps the selector would also run</sub>
