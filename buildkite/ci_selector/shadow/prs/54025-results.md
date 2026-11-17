<!-- ci-selector-shadow -->
### CI selector (shadow): 61 test steps (92 jobs) instead of 100 (145 jobs)

Shadow mode: this changes nothing about what CI runs. It shows what the evidence-based selector would pick for this PR, next to today's rules. [How it works](https://github.com/vllm-project/ci-infra/tree/main/buildkite/ci_selector).

**Feedback welcome:** reply here if it would skip a step this change needs, or runs something unrelated.

| steps (jobs) | Today's rules | Selector | Would skip | Would add |
|---|---|---|---|---|
| NVIDIA, CPU and others | 100 (145) | 61 (92) | 47 (66) | 8 (13) |
| AMD mirrors | 99 (161) | 45 (73) | 58 (94) | 4 (6) |

<details><summary>Selector would run (61)</summary>

- `amd-fp4-moe-kernels-mi355`
- `amd-fp8-moe-kernels-mi355`
- `amd-kernels-mi355`
- `amd-lm-eval-small-models-harness`
- `amd-native-quantization-kernels-mi355`
- `arm-cpu-test` ×3
- `basic-models-test-other-cpu`
- `cpu-kernel-tests` ×2
- `cpu-quantization-model-tests`
- `deepseek-v4-kernel-test-b200`
- `deepseek-v4-kernel-test-h100`
- `distributed-compile-unit-tests-2xh100`
- `distributed-dp-tests-4-gpus`
- `fusion-and-compile-unit-tests-2xb200`
- `fusion-e2e-config-sweep-h100`
- `fusion-e2e-quick-h100`
- `fusion-e2e-tp2-ar-rms-config-sweep-h100`
- `fusion-e2e-tp2-asynctp-config-sweep-h100`
- `fusion-e2e-tp2-b200`
- `fusion-e2e-tp2-quick-h100`
- `kernels-attention-diffkv-test-h100`
- `kernels-attention-test` ×7
- `kernels-b200` ×3
- `kernels-core-operation-test` ×3
- `kernels-deepgemm-test-h100`
- `kernels-fla-ops-test-b200`
- `kernels-flashmla-test-h100`
- `kernels-fusedmoe-layer-test-2-b200s`
- `kernels-fusedmoe-layer-test-2-h100s`
- `kernels-helion-test` ×5
- `kernels-mamba-test`
- `kernels-mhc-test-b200`
- `kernels-minimax-reduce-rms-test-2-gpus`
- `kernels-moe-test` ×5
- `kernels-quantization-test` ×6
- `kernels-root-misc-test-b200`
- `kimi-k3-unit-tests-b200`
- `kv-offload-large`
- `kv-offload-medium`
- `kv-offload-small`
- `lm-eval-dspark-watermark-2xh100`
- `lm-eval-small-models`
- `lm-eval-turboquant-k3v4nc`
- `lm-eval-turboquant-k8v4`
- `lm-eval-turboquant-t3nc`
- `lm-eval-turboquant-t4nc`
- `lm-eval-watermarking`
- `model-executor`
- `mrcr-eval-small-models`
- `plugin-tests-2-gpus`
- `pytorch-compilation-passes-unit-tests`
- `pytorch-compilation-unit-tests`
- `quantization` ×4
- `quantized-fusions`
- `quantized-models-test`
- `quantized-moe-test-b200`
- `qwen4-exp-unit-tests-cpu`
- `v1-attention-b200` ×2
- `v1-attention-h100-mi300` ×2
- `v1-spec-decode`
- `vllm-ir-tests`
</details>

<details><summary>Would skip (today's rules run them) (47)</summary>

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
- `entrypoints-integration-api-server` ×4
- `entrypoints-integration-api-server-generate`
- `entrypoints-integration-api-server-openai-chat_completion`
- `entrypoints-integration-api-server-openai-completion`
- `entrypoints-integration-llm`
- `entrypoints-integration-multimodal`
- `entrypoints-integration-pooling`
- `entrypoints-integration-responses-api`
- `entrypoints-integration-speech_to_text`
- `language-models-tests-granite-l4-compatibility`
- `language-models-tests-hybrid` ×2
- `language-models-tests-standard`
- `lora-tp-distributed` ×4
- `metrics-tracing-2-gpus`
- `multi-modal-models-standard-1-qwen2`
- `multi-modal-models-standard-2-qwen3-gemma`
- `multi-modal-models-standard-3-llava-qwen2-vl`
- `multi-modal-models-standard-4-other-whisper`
- `multi-modal-processor` ×4
- `multi-modal-processor-cpu` ×4
- `pytorch-compilation-dynamic-shapes`
- `pytorch-compilation-unit-tests-h100`
- `pytorch-fullgraph-cudagraph-l4-compatibility`
- `pytorch-fullgraph-test`
- `regression`
- `samplers-multimodal-beam-search`
- `samplers-test`
- `v1-core`
- `v1-executor-worker`
- `v1-kv-connectors` ×4
- `v1-kv-offload`
- `v1-logits-oracle`
- `v1-metrics-lmeval`
- `v1-sample`
</details>

<details><summary>Would add (today's rules do not run them) (8)</summary>

- `arm-cpu-test` ×3 (code map)
- `cpu-kernel-tests` ×2 (code map)
- `cpu-quantization-model-tests` (code map)
- `distributed-dp-tests-4-gpus` (code map)
- `kimi-k3-unit-tests-b200` (code map)
- `qwen4-exp-unit-tests-cpu` (code map)
- `v1-attention-b200` ×2 (code map)
- `v1-attention-h100-mi300` ×2 (code map)
</details>

<details><summary>AMD mirrors: would skip (58)</summary>

- `async-engine-inputs-utils-worker`
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
- `distributed-model-tests-2-gpus` ×3
- `entrypoints-integration-api-server` ×4
- `entrypoints-integration-api-server-generate`
- `entrypoints-integration-api-server-openai-chat_completion`
- `entrypoints-integration-api-server-openai-completion`
- `entrypoints-integration-llm`
- `entrypoints-integration-multimodal`
- `entrypoints-integration-pooling`
- `entrypoints-integration-responses-api`
- `entrypoints-integration-speech_to_text`
- `fault-tolerance-e2e-2xh100`
- `language-models-tests-extra-standard` ×2
- `language-models-tests-granite-l4-compatibility`
- `language-models-tests-hybrid` ×2
- `language-models-tests-standard`
- `lora-tp-distributed` ×4
- `metrics-tracing-2-gpus`
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
- `regression`
- `samplers-multimodal-beam-search`
- `samplers-test`
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
- `v1-kv-connectors` ×4
- `v1-kv-offload`
- `v1-logits-oracle`
- `v1-metrics-lmeval`
- `v1-sample`
</details>

<details><summary>AMD mirrors: would add (4)</summary>

- `kimi-k3-unit-tests-b200` (code map)
- `qwen4-exp-unit-tests` (code map)
- `v1-attention-b200` ×2 (code map)
- `v1-attention-h100-mi300` ×2 (code map)
</details>

#### CI results (2026-09-30 12:58 UTC)

0 passed, 0 failed, 0 pending.

No failures to judge.

<sub>24 changed files · base `70ae0b7435` · head `679385dd22` · Python record: not used (/tmp/ci-infra-selector/buildkite/ci_selector/coverage-data/table.json.gz is table version 5, expected 7; re-merge it from the raw recordings) · kernel record: table d882bddbea (build 91957), map d882bddbea · not counted: 10 build steps, 5 A100 steps the generator no longer emits, 55 optional steps the selector would also run</sub>
