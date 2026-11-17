<!-- ci-selector-shadow -->
### CI selector (shadow): 39 test steps (64 jobs) instead of 70 (90 jobs)

Shadow mode: this changes nothing about what CI runs. It shows what the evidence-based selector would pick for this PR, next to today's rules. [How it works](https://github.com/vllm-project/ci-infra/tree/main/buildkite/ci_selector).

**Feedback welcome:** reply here if it would skip a step this change needs, or runs something unrelated.

| steps (jobs) | Today's rules | Selector | Would skip | Would add |
|---|---|---|---|---|
| NVIDIA, CPU and others | 70 (90) | 39 (64) | 47 (64) | 16 (38) |
| AMD mirrors | 66 (91) | 28 (46) | 47 (69) | 9 (24) |

<details><summary>Selector would run (39)</summary>

- `amd-kernels-mi355`
- `basic-correctness-sleep-mode`
- `cpu-language-generation-and-pooling-model-tests` ×3
- `cpu-multi-modal-model-tests-n` ×4
- `cpu-multimodal-config`
- `distributed-compile-comm-4-gpus`
- `distributed-dp-tests-2-gpus`
- `distributed-dp-tests-4-gpus`
- `distributed-model-tests-2-gpus` ×3
- `distributed-torchrun-shutdown-tests-2-gpus`
- `e2e-core-1-gpu`
- `engine`
- `engine-1-gpu`
- `entrypoints-integration-api-server-openai-completion`
- `entrypoints-integration-multimodal`
- `entrypoints-unit-tests`
- `examples`
- `kernels-attention-test` ×7
- `kernels-b200` ×3
- `kernels-root-misc-test-b200`
- `language-models-tests-extra-standard` ×2
- `language-models-tests-granite-l4-compatibility`
- `language-models-tests-hybrid` ×2
- `language-models-tests-standard`
- `lora` ×4
- `model-executor`
- `multi-modal-models-standard-4-other-whisper`
- `pipeline-context-parallelism-4-gpus`
- `plugin-tests-2-gpus`
- `pytorch-compilation-unit-tests`
- `rayexecutorv2-4-gpus`
- `spec-decode-draft-model` ×4
- `v1-attention-b200` ×2
- `v1-attention-h100-mi300` ×2
- `v1-core`
- `v1-executor-worker`
- `v1-logits-oracle`
- `v1-metrics-lmeval`
- `v1-others-cpu`
</details>

<details><summary>Would skip (today's rules run them) (47)</summary>

- `ascend-npu-test`
- `async-engine-inputs-utils-worker`
- `basic-correctness` ×2
- `basic-correctness-cpu-offload`
- `basic-correctness-cumem`
- `basic-correctness-prefetch-offload`
- `basic-models-test-other-cpu`
- `basic-models-tests-initialization`
- `basic-models-tests-other`
- `benchmarks-cli-test`
- `cpu-params-env-tokenizers-parser`
- `cpu-reasoning-renderers`
- `cpu-tool-parsers`
- `distributed-compile-rpc-tests-2-gpus`
- `e2e-core-large-memory`
- `e2e-scheduling-1-gpu`
- `elastic-ep-scaling-test`
- `entrypoints-integration-api-server` ×4
- `entrypoints-integration-api-server-generate`
- `entrypoints-integration-api-server-openai-chat_completion`
- `entrypoints-integration-llm`
- `entrypoints-integration-pooling`
- `entrypoints-integration-responses-api`
- `entrypoints-integration-speech_to_text`
- `jit-monitor-no-runtime-jit`
- `kernels-fla-ops-test-b200`
- `kernels-fusedmoe-layer-test-2-b200s`
- `kernels-fusedmoe-layer-test-2-h100s`
- `kernels-mhc-test-b200`
- `kernels-moe-test` ×5
- `metrics-tracing-2-gpus`
- `multi-modal-models-standard-1-qwen2`
- `multi-modal-models-standard-2-qwen3-gemma`
- `multi-modal-models-standard-3-llava-qwen2-vl`
- `multi-modal-processor` ×4
- `multi-modal-processor-cpu` ×4
- `pytorch-compilation-dynamic-shapes`
- `pytorch-compilation-passes-unit-tests`
- `pytorch-compilation-unit-tests-h100`
- `pytorch-fullgraph-cudagraph-l4-compatibility`
- `pytorch-fullgraph-test`
- `regression`
- `rust-frontend-distributed`
- `v1-kv-connectors` ×4
- `v1-kv-offload`
- `v1-sample`
- `v1-spec-decode`
</details>

<details><summary>Would add (today's rules do not run them) (16)</summary>

- `amd-kernels-mi355` (code map)
- `cpu-multi-modal-model-tests-n` ×4 (code map)
- `distributed-compile-comm-4-gpus` (code map)
- `distributed-dp-tests-4-gpus` (code map)
- `distributed-model-tests-2-gpus` ×3 (code map)
- `entrypoints-unit-tests` (code map)
- `examples` (code map)
- `kernels-attention-test` ×7 (code map)
- `kernels-b200` ×3 (code map)
- `language-models-tests-extra-standard` ×2 (code map)
- `lora` ×4 (code map)
- `plugin-tests-2-gpus` (code map)
- `rayexecutorv2-4-gpus` (code map)
- `spec-decode-draft-model` ×4 (code map)
- `v1-attention-b200` ×2 (code map)
- `v1-attention-h100-mi300` ×2 (code map)
</details>

<details><summary>AMD mirrors: would skip (47)</summary>

- `async-engine-inputs-utils-worker`
- `basic-correctness` ×2
- `basic-correctness-cpu-offload`
- `basic-correctness-cumem`
- `basic-correctness-prefetch-offload`
- `basic-models-test-other-cpu`
- `basic-models-tests-initialization`
- `basic-models-tests-other`
- `benchmarks-cli-test`
- `deepseek-v4-kernel-test-b200`
- `distributed-compile-rpc-tests-2-gpus`
- `e2e-core-large-memory`
- `e2e-scheduling-1-gpu`
- `entrypoints-integration-api-server` ×4
- `entrypoints-integration-api-server-generate`
- `entrypoints-integration-api-server-openai-chat_completion`
- `entrypoints-integration-llm`
- `entrypoints-integration-pooling`
- `entrypoints-integration-responses-api`
- `entrypoints-integration-speech_to_text`
- `fault-tolerance-e2e-2xh100`
- `jit-monitor-no-runtime-jit`
- `kernels-fla-ops-test-b200`
- `kernels-fp4-moe-test-b200`
- `kernels-fusedmoe-layer-test-2-b200s`
- `kernels-fusedmoe-layer-test-2-h100s`
- `kernels-mhc-test-b200`
- `kernels-moe-test` ×5
- `kernels-quantization-test` ×6
- `kernels-root-misc-test-b200`
- `metrics-tracing-2-gpus`
- `multi-modal-models-standard-1-qwen2`
- `multi-modal-models-standard-2-qwen3-gemma`
- `multi-modal-models-standard-3-llava-qwen2-vl`
- `multi-modal-processor` ×4
- `multi-modal-processor-cpu` ×4
- `platform-tests`
- `pytorch-compilation-dynamic-shapes`
- `pytorch-compilation-passes-unit-tests`
- `pytorch-compilation-unit-tests-h100`
- `pytorch-fullgraph-test`
- `regression`
- `rust-frontend-distributed`
- `v1-kv-connectors` ×4
- `v1-kv-offload`
- `v1-sample`
- `v1-spec-decode`
</details>

<details><summary>AMD mirrors: would add (9)</summary>

- `entrypoints-unit-tests` (code map)
- `examples` (code map)
- `kernels-attention-test` ×7 (code map)
- `language-models-tests-extra-standard` ×2 (code map)
- `lora` ×4 (code map)
- `plugin-tests-2-gpus` (code map)
- `spec-decode-draft-model` ×4 (code map)
- `v1-attention-b200` ×2 (code map)
- `v1-attention-h100-mi300` ×2 (code map)
</details>

#### CI results (2026-09-30 16:52 UTC)

143 passed, 1 failed, 30 pending.
CI is still running; the picture below is not final.

No misses: every failed job the selector would skip was also failing on main.

- `computer-cpu-reasoning-plus-renderers`: selector would skip it; also failing on main (`cff08b461e`, `ff1b87cca2`, `0f8b398158`), pre-existing

<sub>8 changed files · base `cff08b461e` · head `79ce2746a6` · Python record: not used (/tmp/ci-infra-selector/buildkite/ci_selector/coverage-data/table.json.gz is table version 5, expected 7; re-merge it from the raw recordings) · kernel record: table 866fa130fa (build 92059), map 866fa130fa · not counted: 10 build steps, 5 A100 steps the generator no longer emits, 16 optional steps the selector would also run</sub>
