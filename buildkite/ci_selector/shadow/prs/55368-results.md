<!-- ci-selector-shadow -->
### CI selector (shadow): 7 test steps (13 jobs) instead of 65 (88 jobs)

Shadow mode: this changes nothing about what CI runs. It shows what the evidence-based selector would pick for this PR, next to today's rules. [How it works](https://github.com/vllm-project/ci-infra/tree/main/buildkite/ci_selector).

**Feedback welcome:** reply here if it would skip a step this change needs, or runs something unrelated.

| steps (jobs) | Today's rules | Selector | Would skip | Would add |
|---|---|---|---|---|
| NVIDIA, CPU and others | 65 (88) | 7 (13) | 59 (78) | 1 (3) |
| AMD mirrors | 78 (121) | 11 (17) | 70 (109) | 3 (5) |

<details><summary>Selector would run (7)</summary>

- `amd-fp8-moe-kernels-mi355`
- `basic-models-test-other-cpu`
- `kernels-b200` ×3
- `kernels-fusedmoe-layer-test-2-b200s`
- `kernels-moe-test` ×5
- `kernels-root-misc-test-b200`
- `model-executor`
</details>

<details><summary>Would skip (today's rules run them) (59)</summary>

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
- `distributed-compile-unit-tests-2xh100`
- `entrypoints-integration-api-server` ×4
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
- `kernels-deepgemm-test-h100`
- `kernels-fla-ops-test-b200`
- `kernels-fusedmoe-layer-test-2-h100s`
- `kernels-mhc-test-b200`
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
- `pytorch-compilation-passes-unit-tests`
- `pytorch-compilation-unit-tests`
- `pytorch-compilation-unit-tests-h100`
- `pytorch-fullgraph-cudagraph-l4-compatibility`
- `pytorch-fullgraph-test`
- `quantized-moe-test-b200`
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
- `v1-spec-decode`
</details>

<details><summary>Would add (today's rules do not run them) (1)</summary>

- `kernels-b200` ×3 (code map)
</details>

<details><summary>AMD mirrors: would skip (70)</summary>

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
- `deepseek-v4-kernel-test-b200`
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
- `fusion-and-compile-unit-tests-2xb200`
- `fusion-e2e-quick-h100`
- `fusion-e2e-tp2-ar-rms-config-sweep-h100`
- `fusion-e2e-tp2-b200`
- `fusion-e2e-tp2-quick-h100`
- `kernels-fla-ops-test-b200`
- `kernels-mhc-test-b200`
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
- `pytorch-compilation-passes-unit-tests`
- `pytorch-compilation-unit-tests`
- `pytorch-compilation-unit-tests-h100`
- `pytorch-fullgraph-test`
- `quantization` ×4
- `quantized-moe-test-b200`
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
- `v1-spec-decode`
</details>

<details><summary>AMD mirrors: would add (3)</summary>

- `deepseek-v4-kernel-test-h100` (code map)
- `gemm-rs-ar-2xb200` (code map)
- `kernels-core-operation-test` ×3 (code map)
</details>

#### CI results (2026-09-30 15:52 UTC)

166 passed, 0 failed, 0 pending.

No failures to judge.

<sub>4 changed files · base `2df122e65e` · head `4e1182a3a6` · Python record: not used (/tmp/ci-infra-selector/buildkite/ci_selector/coverage-data/table.json.gz is table version 5, expected 7; re-merge it from the raw recordings) · kernel record: table 866fa130fa (build 92059), map 866fa130fa · not counted: 10 build steps, 5 A100 steps the generator no longer emits, 2 optional steps the selector would also run</sub>
