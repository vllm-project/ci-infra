<!-- ci-selector-shadow -->
### CI selector (shadow): 42 test steps (77 jobs) instead of 67 (102 jobs)

Shadow mode: this changes nothing about what CI runs. It shows what the evidence-based selector would pick for this PR, next to today's rules. [How it works](https://github.com/vllm-project/ci-infra/tree/main/buildkite/ci_selector).

**Feedback welcome:** reply here if it would skip a step this change needs, or runs something unrelated.

| steps (jobs) | Today's rules | Selector | Would skip | Would add |
|---|---|---|---|---|
| NVIDIA, CPU and others | 67 (102) | 42 (77) | 45 (55) | 20 (30) |
| AMD mirrors | 66 (99) | 32 (60) | 45 (55) | 11 (16) |

<details><summary>Selector would run (42)</summary>

- `arm-cpu-test` ×3
- `async-engine-inputs-utils-worker`
- `basic-models-test-other-cpu`
- `basic-models-tests-extra-initialization` ×14
- `basic-models-tests-initialization`
- `basic-models-tests-other`
- `batch-invariance-b200`
- `batch-invariance-h100`
- `cpu-language-generation-and-pooling-model-tests` ×3
- `cpu-multi-modal-model-tests-n` ×4
- `cpu-multimodal-config`
- `cpu-params-env-tokenizers-parser`
- `cpu-reasoning-renderers`
- `crosslayer-kv-layout-distributed-nixlconnector-pd-accuracy-tests-4-gpus`
- `distributed-flashinfer-nixlconnector-pd-accuracy-4-gpus`
- `distributed-model-tests-2-gpus` ×3
- `distributed-nixlconnector-pd-accuracy-4-gpus`
- `dp-ep-distributed-nixlconnector-pd-accuracy-tests-4-gpus`
- `e2e-core-1-gpu`
- `engine`
- `entrypoints-integration-pooling`
- `examples`
- `hybrid-ssm-nixlconnector-pd-accuracy-tests-4-gpus`
- `kernels-core-operation-test` ×3
- `kernels-root-misc-test-b200`
- `language-models-tests-extra-standard` ×2
- `language-models-tests-granite-l4-compatibility`
- `language-models-tests-hybrid` ×2
- `language-models-tests-standard`
- `lora` ×4
- `model-executor`
- `multi-modal-models-standard-1-qwen2`
- `multi-modal-models-standard-2-qwen3-gemma`
- `multi-modal-models-standard-3-llava-qwen2-vl`
- `multi-modal-models-standard-4-other-whisper`
- `multi-modal-processor-cpu` ×4
- `multiconnector-nixl-offloading-pd-accuracy-2-gpus`
- `pipeline-context-parallelism-4-gpus`
- `push-nixlconnector-pp-prefill-pd-accuracy-4-gpus`
- `rayexecutorv2-4-gpus`
- `spec-decode-draft-model` ×4
- `v1-sample`
</details>

<details><summary>Would skip (today's rules run them) (45)</summary>

- `amd-lm-eval-small-models-harness`
- `ascend-npu-test`
- `basic-correctness` ×2
- `basic-correctness-cpu-offload`
- `basic-correctness-cumem`
- `basic-correctness-prefetch-offload`
- `basic-correctness-sleep-mode`
- `benchmarks-cli-test`
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
- `kernels-mhc-test-b200`
- `metrics-tracing-2-gpus`
- `multi-modal-processor` ×4
- `pytorch-compilation-dynamic-shapes`
- `pytorch-compilation-passes-unit-tests`
- `pytorch-compilation-unit-tests`
- `pytorch-compilation-unit-tests-h100`
- `pytorch-fullgraph-cudagraph-l4-compatibility`
- `pytorch-fullgraph-test`
- `regression`
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
- `v1-others-cpu`
- `v1-spec-decode`
</details>

<details><summary>Would add (today's rules do not run them) (20)</summary>

- `arm-cpu-test` ×3 (code map)
- `batch-invariance-b200` (code map)
- `batch-invariance-h100` (code map)
- `cpu-multi-modal-model-tests-n` ×4 (code map)
- `cpu-multimodal-config` (code map)
- `cpu-params-env-tokenizers-parser` (code map)
- `cpu-reasoning-renderers` (code map)
- `crosslayer-kv-layout-distributed-nixlconnector-pd-accuracy-tests-4-gpus` (code map)
- `distributed-flashinfer-nixlconnector-pd-accuracy-4-gpus` (code map)
- `distributed-nixlconnector-pd-accuracy-4-gpus` (code map)
- `dp-ep-distributed-nixlconnector-pd-accuracy-tests-4-gpus` (code map)
- `e2e-core-1-gpu` (code map)
- `engine` (code map)
- `examples` (code map)
- `hybrid-ssm-nixlconnector-pd-accuracy-tests-4-gpus` (code map)
- `kernels-core-operation-test` ×3 (code map)
- `lora` ×4 (code map)
- `multiconnector-nixl-offloading-pd-accuracy-2-gpus` (code map)
- `push-nixlconnector-pp-prefill-pd-accuracy-4-gpus` (code map)
- `rayexecutorv2-4-gpus` (code map)
</details>

<details><summary>AMD mirrors: would skip (45)</summary>

- `basic-correctness` ×2
- `basic-correctness-cpu-offload`
- `basic-correctness-cumem`
- `basic-correctness-prefetch-offload`
- `basic-correctness-sleep-mode`
- `benchmarks-cli-test`
- `entrypoints-integration-api-server` ×4
- `entrypoints-integration-api-server-generate`
- `entrypoints-integration-api-server-openai-chat_completion`
- `entrypoints-integration-api-server-openai-completion`
- `entrypoints-integration-llm`
- `entrypoints-integration-multimodal`
- `entrypoints-integration-responses-api`
- `entrypoints-integration-speech_to_text`
- `fusion-e2e-quick-h100`
- `fusion-e2e-tp2-ar-rms-config-sweep-h100`
- `fusion-e2e-tp2-b200`
- `fusion-e2e-tp2-quick-h100`
- `kernels-fla-ops-test-b200`
- `kernels-mhc-test-b200`
- `lm-eval-small-models`
- `metrics-tracing-2-gpus`
- `mrcr-eval-small-models`
- `multi-modal-processor` ×4
- `platform-tests`
- `pytorch-compilation-dynamic-shapes`
- `pytorch-compilation-passes-unit-tests`
- `pytorch-compilation-unit-tests`
- `pytorch-compilation-unit-tests-h100`
- `pytorch-fullgraph-test`
- `regression`
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
- `v1-spec-decode`
</details>

<details><summary>AMD mirrors: would add (11)</summary>

- `crosslayer-kv-layout-distributed-nixlconnector-pd-accuracy-tests-4-gpus` (code map)
- `distributed-nixlconnector-pd-accuracy-4-gpus` (code map)
- `dp-ep-distributed-nixlconnector-pd-accuracy-tests-4-gpus` (code map)
- `e2e-core-1-gpu` (code map)
- `engine` (code map)
- `examples` (code map)
- `hybrid-ssm-nixlconnector-pd-accuracy-tests-4-gpus` (code map)
- `kernels-core-operation-test` ×3 (code map)
- `lora` ×4 (code map)
- `multiconnector-nixl-offloading-pd-accuracy-2-gpus` (code map)
- `push-nixlconnector-pp-prefill-pd-accuracy-4-gpus` (code map)
</details>

#### CI results (2026-09-30 12:52 UTC)

161 passed, 0 failed, 0 pending.

No failures to judge.

<sub>2 changed files · base `d71f662606` · head `de4e6c4b91` · Python record: not used (/tmp/ci-infra-selector/buildkite/ci_selector/coverage-data/table.json.gz is table version 5, expected 7; re-merge it from the raw recordings) · kernel record: table d882bddbea (build 91957), map d882bddbea · not counted: 10 build steps, 5 A100 steps the generator no longer emits, 39 optional steps the selector would also run</sub>
