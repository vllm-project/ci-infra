<!-- ci-selector-shadow -->
### CI selector (shadow): 37 test steps (64 jobs) instead of 69 (87 jobs)

Shadow mode: this changes nothing about what CI runs. It shows what the evidence-based selector would pick for this PR, next to today's rules. [How it works](https://github.com/vllm-project/ci-infra/tree/main/buildkite/ci_selector).

**Feedback welcome:** reply here if it would skip a step this change needs, or runs something unrelated.

| steps (jobs) | Today's rules | Selector | Would skip | Would add |
|---|---|---|---|---|
| NVIDIA, CPU and others | 69 (87) | 37 (64) | 54 (67) | 22 (44) |
| AMD mirrors | 62 (78) | 32 (62) | 46 (57) | 16 (41) |

<details><summary>Selector would run (37)</summary>

- `amd-fp8-moe-kernels-mi355`
- `async-engine-inputs-utils-worker`
- `basic-correctness-cumem`
- `basic-correctness-sleep-mode`
- `basic-models-tests-other`
- `cpu-params-env-tokenizers-parser`
- `cpu-spec-decode-tests`
- `cudagraph`
- `deepseek-v4-kernel-test-h100`
- `distributed-comm-ops`
- `distributed-compile-unit-tests-2xh100`
- `distributed-model-tests-2-gpus` ×3
- `distributed-torchrun-shutdown-tests-2-gpus`
- `engine-1-gpu`
- `fusion-and-compile-unit-tests-2xb200`
- `glm5next-unit-tests`
- `kernels-attention-test` ×7
- `kernels-b200` ×3
- `kernels-flashmla-test-h100`
- `kernels-fusedmoe-layer-test-2-b200s`
- `kernels-fusedmoe-layer-test-2-h100s`
- `kernels-mamba-test`
- `kernels-moe-test` ×5
- `kernels-quantization-test` ×6
- `kernels-root-misc-test-b200`
- `kimi-k3-unit-tests-b200`
- `lora` ×4
- `model-executor`
- `pytorch-compilation-passes-unit-tests`
- `pytorch-compilation-unit-tests`
- `v1-attention-b200` ×2
- `v1-attention-h100-mi300` ×2
- `v1-core`
- `v1-executor-worker`
- `v1-kv-connectors` ×4
- `v1-kv-offload`
- `v1-others-cpu`
</details>

<details><summary>Would skip (today's rules run them) (54)</summary>

- `ascend-npu-test`
- `basic-correctness` ×2
- `basic-correctness-cpu-offload`
- `basic-correctness-prefetch-offload`
- `basic-models-test-other-cpu`
- `basic-models-tests-initialization`
- `benchmarks-cli-test`
- `cpu-distributed-tests-dp-tp`
- `cpu-distributed-tests-pp-tp`
- `cpu-language-generation-and-pooling-model-tests` ×3
- `cpu-multimodal-config`
- `cpu-reasoning-renderers`
- `cpu-tool-parsers`
- `distributed-compile-rpc-tests-2-gpus`
- `distributed-dp-tests-2-gpus`
- `distributed-tests-8xh100`
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
- `entrypoints-integration-speech_to_text`
- `fault-tolerance-e2e-2xh100`
- `jit-monitor-no-runtime-jit`
- `kernels-fla-ops-test-b200`
- `kernels-mhc-test-b200`
- `language-models-tests-granite-l4-compatibility`
- `language-models-tests-hybrid` ×2
- `language-models-tests-standard`
- `metrics-tracing-2-gpus`
- `mooncake-ec-tcp-e2e-2-gpus`
- `multi-modal-models-standard-1-qwen2`
- `multi-modal-models-standard-2-qwen3-gemma`
- `multi-modal-models-standard-3-llava-qwen2-vl`
- `multi-modal-models-standard-4-other-whisper`
- `multi-modal-processor` ×4
- `multi-modal-processor-cpu` ×4
- `pipeline-context-parallelism-4-gpus`
- `pytorch-compilation-dynamic-shapes`
- `pytorch-compilation-unit-tests-h100`
- `pytorch-fullgraph-cudagraph-l4-compatibility`
- `pytorch-fullgraph-test`
- `regression`
- `rust-frontend-distributed`
- `v1-logits-oracle`
- `v1-metrics-lmeval`
- `v1-sample`
- `v1-spec-decode`
</details>

<details><summary>Would add (today's rules do not run them) (22)</summary>

- `amd-fp8-moe-kernels-mi355` (code map)
- `cpu-spec-decode-tests` (code map)
- `cudagraph` (code map)
- `deepseek-v4-kernel-test-h100` (code map)
- `distributed-comm-ops` (code map)
- `distributed-compile-unit-tests-2xh100` (code map)
- `engine-1-gpu` (code map)
- `fusion-and-compile-unit-tests-2xb200` (code map)
- `glm5next-unit-tests` (code map)
- `kernels-attention-test` ×7 (code map)
- `kernels-b200` ×3 (code map)
- `kernels-flashmla-test-h100` (code map)
- `kernels-fusedmoe-layer-test-2-b200s` (code map)
- `kernels-fusedmoe-layer-test-2-h100s` (code map)
- `kernels-mamba-test` (code map)
- `kernels-moe-test` ×5 (code map)
- `kernels-quantization-test` ×6 (code map)
- `kimi-k3-unit-tests-b200` (code map)
- `lora` ×4 (code map)
- `model-executor` (code map)
- `v1-attention-b200` ×2 (code map)
- `v1-attention-h100-mi300` ×2 (code map)
</details>

<details><summary>AMD mirrors: would skip (46)</summary>

- `basic-correctness` ×2
- `basic-correctness-cpu-offload`
- `basic-correctness-prefetch-offload`
- `basic-models-test-other-cpu`
- `basic-models-tests-initialization`
- `benchmarks-cli-test`
- `distributed-compile-rpc-tests-2-gpus`
- `distributed-dp-tests-2-gpus`
- `distributed-tests-8xh100`
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
- `entrypoints-integration-speech_to_text`
- `jit-monitor-no-runtime-jit`
- `kernels-fla-ops-test-b200`
- `kernels-mhc-test-b200`
- `language-models-tests-granite-l4-compatibility`
- `language-models-tests-hybrid` ×2
- `language-models-tests-standard`
- `metrics-tracing-2-gpus`
- `mooncake-ec-tcp-e2e-2-gpus`
- `multi-modal-models-standard-1-qwen2`
- `multi-modal-models-standard-2-qwen3-gemma`
- `multi-modal-models-standard-3-llava-qwen2-vl`
- `multi-modal-models-standard-4-other-whisper`
- `multi-modal-processor` ×4
- `multi-modal-processor-cpu` ×4
- `pipeline-context-parallelism-4-gpus`
- `platform-tests`
- `pytorch-compilation-dynamic-shapes`
- `pytorch-compilation-unit-tests-h100`
- `pytorch-fullgraph-test`
- `regression`
- `rust-frontend-distributed`
- `v1-logits-oracle`
- `v1-metrics-lmeval`
- `v1-sample`
- `v1-spec-decode`
</details>

<details><summary>AMD mirrors: would add (16)</summary>

- `cudagraph` (code map)
- `engine-1-gpu` (code map)
- `glm5next-unit-tests` (code map)
- `kernels-attention-test` ×7 (code map)
- `kernels-core-operation-test` ×3 (code map)
- `kernels-flashmla-test-h100` (code map)
- `kernels-fp4-moe-test-b200` (code map)
- `kernels-mamba-test` (code map)
- `kernels-moe-test` ×5 (code map)
- `kernels-quantization-test` ×6 (code map)
- `kimi-k3-unit-tests-b200` (code map)
- `lora` ×4 (code map)
- `model-executor` (code map)
- `quantization` ×4 (code map)
- `v1-attention-b200` ×2 (code map)
- `v1-attention-h100-mi300` ×2 (code map)
</details>

#### CI results (2026-09-30 09:24 UTC)

0 passed, 0 failed, 0 pending.

No failures to judge.

<sub>7 changed files · base `b81984879a` · head `6f879bb376` · Python record: not used (/tmp/ci-infra-selector/buildkite/ci_selector/coverage-data/table.json.gz is table version 5, expected 7; re-merge it from the raw recordings) · kernel record: table d882bddbea (build 91957), map d882bddbea · not counted: 10 build steps, 5 A100 steps the generator no longer emits, 14 optional steps the selector would also run</sub>
