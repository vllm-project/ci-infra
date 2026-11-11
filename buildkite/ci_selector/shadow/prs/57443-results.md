<!-- ci-selector-shadow -->
### CI selector (shadow): 44 test steps (93 jobs) instead of 83 (110 jobs)

Shadow mode: this changes nothing about what CI runs. It shows what the evidence-based selector would pick for this PR, next to today's rules. [How it works](https://github.com/vllm-project/ci-infra/tree/main/buildkite/ci_selector).

**Feedback welcome:** reply here if it would skip a step this change needs, or runs something unrelated.

| steps (jobs) | Today's rules | Selector | Would skip | Would add |
|---|---|---|---|---|
| NVIDIA, CPU and others | 83 (110) | 44 (93) | 56 (63) | 17 (46) |
| AMD mirrors | 82 (110) | 31 (70) | 59 (66) | 8 (26) |

<details><summary>Selector would run (44)</summary>

- `amd-kernels-mi355`
- `arm-cpu-test` ×3
- `basic-models-test-other-cpu`
- `basic-models-tests-extra-initialization` ×14
- `basic-models-tests-initialization`
- `basic-models-tests-other`
- `cpu-kernel-tests` ×2
- `cpu-language-generation-and-pooling-model-tests` ×3
- `cpu-multi-modal-model-tests-n` ×4
- `cpu-params-env-tokenizers-parser`
- `cpu-reasoning-renderers`
- `cpu-spec-decode-tests`
- `distributed-model-tests-2-gpus` ×3
- `distributed-torchrun-shutdown-tests-2-gpus`
- `entrypoints-integration-speech_to_text`
- `fusion-and-compile-unit-tests-2xb200`
- `glm5next-unit-tests`
- `kernels-attention-test` ×7
- `kernels-b200` ×3
- `kernels-core-operation-test` ×3
- `kernels-flashmla-test-h100`
- `kernels-root-misc-test-b200`
- `kimi-k3-unit-tests-b200`
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
- `pytorch-compilation-passes-unit-tests`
- `pytorch-compilation-unit-tests`
- `quantization` ×4
- `qwen4-exp-unit-tests`
- `rayexecutorv2-4-gpus`
- `v1-attention-b200` ×2
- `v1-attention-h100-mi300` ×2
- `v1-executor-worker`
- `v1-kv-connectors` ×4
- `v1-others-cpu`
- `v1-spec-decode`
</details>

<details><summary>Would skip (today's rules run them) (56)</summary>

- `amd-lm-eval-small-models-harness`
- `ascend-npu-test`
- `async-engine-inputs-utils-worker`
- `basic-correctness` ×2
- `basic-correctness-cpu-offload`
- `basic-correctness-cumem`
- `basic-correctness-prefetch-offload`
- `basic-correctness-sleep-mode`
- `batch-invariance-b200`
- `batch-invariance-h100`
- `benchmarks-cli-test`
- `cpu-multimodal-config`
- `cpu-tool-parsers`
- `distributed-compile-rpc-tests-2-gpus`
- `distributed-dp-tests-2-gpus`
- `e2e-core-1-gpu`
- `e2e-core-large-memory`
- `e2e-scheduling-1-gpu`
- `entrypoints-integration-api-server` ×4
- `entrypoints-integration-api-server-generate`
- `entrypoints-integration-api-server-openai-chat_completion`
- `entrypoints-integration-api-server-openai-completion`
- `entrypoints-integration-llm`
- `entrypoints-integration-multimodal`
- `entrypoints-integration-pooling`
- `entrypoints-integration-responses-api`
- `fault-tolerance-e2e-2xh100`
- `fusion-e2e-quick-h100`
- `fusion-e2e-tp2-b200`
- `fusion-e2e-tp2-quick-h100`
- `kernels-fla-ops-test-b200`
- `kernels-mhc-test-b200`
- `lm-eval-dspark-watermark-2xh100`
- `lm-eval-watermarking`
- `metrics-tracing-2-gpus`
- `multi-modal-models-standard-2-qwen3-gemma`
- `multi-modal-models-standard-3-llava-qwen2-vl`
- `pytorch-compilation-dynamic-shapes`
- `pytorch-compilation-unit-tests-h100`
- `pytorch-fullgraph-cudagraph-l4-compatibility`
- `pytorch-fullgraph-test`
- `regression`
- `rust-frontend-distributed`
- `spec-decode-draft-model` ×4
- `spec-decode-eagle-1-deepseek-qwen`
- `spec-decode-eagle-2-llama3-qwen-vl-other`
- `spec-decode-mtp-deepseek-mimo`
- `spec-decode-mtp-gemma4`
- `spec-decode-mtp-qwen3-5`
- `spec-decode-ngram-suffix`
- `spec-decode-speculators`
- `v1-core`
- `v1-kv-offload`
- `v1-logits-oracle`
- `v1-metrics-lmeval`
- `v1-sample`
</details>

<details><summary>Would add (today's rules do not run them) (17)</summary>

- `amd-kernels-mi355` (code map)
- `arm-cpu-test` ×3 (code map)
- `basic-models-tests-extra-initialization` ×14 (code map)
- `cpu-kernel-tests` ×2 (code map)
- `cpu-multi-modal-model-tests-n` ×4 (code map)
- `distributed-model-tests-2-gpus` ×3 (code map)
- `fusion-and-compile-unit-tests-2xb200` (code map)
- `glm5next-unit-tests` (code map)
- `kernels-b200` ×3 (code map)
- `kernels-core-operation-test` ×3 (code map)
- `kernels-flashmla-test-h100` (code map)
- `kimi-k3-unit-tests-b200` (code map)
- `language-models-tests-extra-standard` ×2 (code map)
- `model-executor` (code map)
- `quantization` ×4 (code map)
- `qwen4-exp-unit-tests` (code map)
- `rayexecutorv2-4-gpus` (code map)
</details>

<details><summary>AMD mirrors: would skip (59)</summary>

- `async-engine-inputs-utils-worker`
- `basic-correctness` ×2
- `basic-correctness-cpu-offload`
- `basic-correctness-cumem`
- `basic-correctness-prefetch-offload`
- `basic-correctness-sleep-mode`
- `batch-invariance-h100`
- `benchmarks-cli-test`
- `distributed-compile-rpc-tests-2-gpus`
- `distributed-dp-tests-2-gpus`
- `e2e-core-1-gpu`
- `e2e-core-large-memory`
- `e2e-scheduling-1-gpu`
- `entrypoints-integration-api-server` ×4
- `entrypoints-integration-api-server-generate`
- `entrypoints-integration-api-server-openai-chat_completion`
- `entrypoints-integration-api-server-openai-completion`
- `entrypoints-integration-llm`
- `entrypoints-integration-multimodal`
- `entrypoints-integration-pooling`
- `entrypoints-integration-responses-api`
- `extract-hidden-states-integration-2-gpus`
- `fault-tolerance-e2e-2xh100`
- `fusion-and-compile-unit-tests-2xb200`
- `fusion-e2e-quick-h100`
- `fusion-e2e-tp2-ar-rms-config-sweep-h100`
- `fusion-e2e-tp2-b200`
- `fusion-e2e-tp2-quick-h100`
- `jit-monitor-no-runtime-jit`
- `kernels-fla-ops-test-b200`
- `kernels-mhc-test-b200`
- `lm-eval-dspark-watermark-2xh100`
- `lm-eval-small-models`
- `lm-eval-watermarking`
- `metrics-tracing-2-gpus`
- `mrcr-eval-small-models`
- `multi-modal-models-standard-2-qwen3-gemma`
- `multi-modal-models-standard-3-llava-qwen2-vl`
- `openai-api-correctness`
- `platform-tests`
- `pytorch-compilation-dynamic-shapes`
- `pytorch-compilation-unit-tests-h100`
- `pytorch-fullgraph-test`
- `quantized-moe-test-b200`
- `regression`
- `rust-frontend-distributed`
- `spec-decode-draft-model` ×4
- `spec-decode-eagle-1-deepseek-qwen`
- `spec-decode-eagle-2-llama3-qwen-vl-other`
- `spec-decode-mtp-deepseek-mimo`
- `spec-decode-mtp-gemma4`
- `spec-decode-mtp-qwen3-5`
- `spec-decode-ngram-suffix`
- `spec-decode-speculators`
- `v1-core`
- `v1-kv-offload`
- `v1-logits-oracle`
- `v1-metrics-lmeval`
- `v1-sample`
</details>

<details><summary>AMD mirrors: would add (8)</summary>

- `basic-models-tests-extra-initialization` ×14 (code map)
- `fusion-e2e-tp2-asynctp-config-sweep-h100` (code map)
- `glm5next-unit-tests` (code map)
- `kernels-core-operation-test` ×3 (code map)
- `kernels-flashmla-test-h100` (code map)
- `kimi-k3-unit-tests-b200` (code map)
- `model-executor` (code map)
- `quantization` ×4 (code map)
</details>

#### CI results (2026-09-30 14:43 UTC)

11 passed, 1 failed, 0 pending.

No misses: the selector would have run every failed job.

- `github-github-pre-commit-check`: no step matches this job, not judged

<sub>5 changed files · base `b22494cc0c` · head `34b6b98a15` · Python record: not used (/tmp/ci-infra-selector/buildkite/ci_selector/coverage-data/table.json.gz is table version 5, expected 7; re-merge it from the raw recordings) · kernel record: table 866fa130fa (build 92059), map 866fa130fa · not counted: 10 build steps, 5 A100 steps the generator no longer emits, 21 optional steps the selector would also run</sub>
