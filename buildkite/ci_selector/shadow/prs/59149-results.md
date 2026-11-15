<!-- ci-selector-shadow -->
### CI selector (shadow): 11 test steps (22 jobs) instead of 62 (84 jobs)

Shadow mode: this changes nothing about what CI runs. It shows what the evidence-based selector would pick for this PR, next to today's rules. [How it works](https://github.com/vllm-project/ci-infra/tree/main/buildkite/ci_selector).

**Feedback welcome:** reply here if it would skip a step this change needs, or runs something unrelated.

| steps (jobs) | Today's rules | Selector | Would skip | Would add |
|---|---|---|---|---|
| NVIDIA, CPU and others | 62 (84) | 11 (22) | 57 (73) | 6 (11) |
| AMD mirrors | 64 (90) | 2 (5) | 62 (85) | 0 (0) |

<details><summary>Selector would run (11)</summary>

- `arm-cpu-test` ×3
- `cpu-distributed-tests-dp-tp`
- `cpu-distributed-tests-pp-tp`
- `cpu-kernel-tests` ×2
- `cpu-language-generation-and-pooling-model-tests` ×3
- `cpu-multi-modal-model-tests-n` ×4
- `cpu-quantization-model-tests`
- `cpu-qwen2-5-vl-multimodal-tests`
- `cpu-spec-decode-tests`
- `model-executor`
- `quantization` ×4
</details>

<details><summary>Would skip (today's rules run them) (57)</summary>

- `amd-lm-eval-small-models-harness`
- `amd-native-quantization-kernels-mi355`
- `async-engine-inputs-utils-worker`
- `basic-correctness` ×2
- `basic-correctness-cpu-offload`
- `basic-correctness-cumem`
- `basic-correctness-prefetch-offload`
- `basic-correctness-sleep-mode`
- `basic-models-test-other-cpu`
- `basic-models-tests-initialization`
- `basic-models-tests-other`
- `benchmarks-cli-test`
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
- `kernels-core-operation-test` ×3
- `kernels-fla-ops-test-b200`
- `kernels-mhc-test-b200`
- `kernels-root-misc-test-b200`
- `language-models-tests-granite-l4-compatibility`
- `language-models-tests-hybrid` ×2
- `language-models-tests-standard`
- `lm-eval-small-models`
- `metrics-tracing-2-gpus`
- `multi-modal-models-standard-1-qwen2`
- `multi-modal-models-standard-2-qwen3-gemma`
- `multi-modal-models-standard-3-llava-qwen2-vl`
- `multi-modal-models-standard-4-other-whisper`
- `multi-modal-processor` ×4
- `multi-modal-processor-cpu` ×4
- `openai-api-correctness`
- `pytorch-compilation-dynamic-shapes`
- `pytorch-compilation-passes-unit-tests`
- `pytorch-compilation-unit-tests`
- `pytorch-compilation-unit-tests-h100`
- `pytorch-fullgraph-cudagraph-l4-compatibility`
- `pytorch-fullgraph-test`
- `quantized-fusions`
- `regression`
- `torch-stable-abi-audit`
- `v1-core`
- `v1-executor-worker`
- `v1-kv-connectors` ×4
- `v1-kv-offload`
- `v1-logits-oracle`
- `v1-metrics-lmeval`
- `v1-sample`
- `v1-spec-decode`
</details>

<details><summary>Would add (today's rules do not run them) (6)</summary>

- `arm-cpu-test` ×3 (code map)
- `cpu-distributed-tests-dp-tp` (code map)
- `cpu-distributed-tests-pp-tp` (code map)
- `cpu-multi-modal-model-tests-n` ×4 (code map)
- `cpu-qwen2-5-vl-multimodal-tests` (code map)
- `cpu-spec-decode-tests` (code map)
</details>

<details><summary>AMD mirrors: would skip (62)</summary>

- `async-engine-inputs-utils-worker`
- `basic-correctness` ×2
- `basic-correctness-cpu-offload`
- `basic-correctness-cumem`
- `basic-correctness-prefetch-offload`
- `basic-correctness-sleep-mode`
- `basic-models-test-other-cpu`
- `basic-models-tests-initialization`
- `basic-models-tests-other`
- `benchmarks-cli-test`
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
- `fusion-e2e-tp2-asynctp-config-sweep-h100`
- `fusion-e2e-tp2-b200`
- `fusion-e2e-tp2-quick-h100`
- `gemm-rs-ar-2xb200`
- `kernels-b200` ×3
- `kernels-core-operation-test` ×3
- `kernels-fla-ops-test-b200`
- `kernels-mhc-test-b200`
- `kernels-quantization-test` ×6
- `kernels-root-misc-test-b200`
- `language-models-tests-granite-l4-compatibility`
- `language-models-tests-hybrid` ×2
- `language-models-tests-standard`
- `lm-eval-small-models`
- `metrics-tracing-2-gpus`
- `multi-modal-models-standard-1-qwen2`
- `multi-modal-models-standard-2-qwen3-gemma`
- `multi-modal-models-standard-3-llava-qwen2-vl`
- `multi-modal-models-standard-4-other-whisper`
- `multi-modal-processor` ×4
- `multi-modal-processor-cpu` ×4
- `openai-api-correctness`
- `platform-tests`
- `pytorch-compilation-dynamic-shapes`
- `pytorch-compilation-passes-unit-tests`
- `pytorch-compilation-unit-tests`
- `pytorch-compilation-unit-tests-h100`
- `pytorch-fullgraph-test`
- `quantized-fusions`
- `quantized-moe-test-b200`
- `regression`
- `torch-stable-abi-audit`
- `v1-core`
- `v1-executor-worker`
- `v1-kv-connectors` ×4
- `v1-kv-offload`
- `v1-logits-oracle`
- `v1-metrics-lmeval`
- `v1-sample`
- `v1-spec-decode`
</details>

<details><summary>AMD mirrors: would add (0)</summary>

none
</details>

#### CI results (2026-09-30 14:45 UTC)

165 passed, 0 failed, 0 pending.

No failures to judge.

<sub>7 changed files · base `863475eb9c` · head `72e7874fa6` · Python record: not used (/tmp/ci-infra-selector/buildkite/ci_selector/coverage-data/table.json.gz is table version 5, expected 7; re-merge it from the raw recordings) · kernel record: table 866fa130fa (build 92059), map 866fa130fa · not counted: 10 build steps, 5 A100 steps the generator no longer emits, 1 optional steps the selector would also run</sub>
