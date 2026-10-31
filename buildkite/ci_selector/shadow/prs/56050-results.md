<!-- ci-selector-shadow -->
### CI selector (shadow): 60 test steps (115 jobs) instead of 65 (85 jobs)

Shadow mode: this changes nothing about what CI runs. It shows what the evidence-based selector would pick for this PR, next to today's rules. [How it works](https://github.com/vllm-project/ci-infra/tree/main/buildkite/ci_selector).

**Feedback welcome:** reply here if it would skip a step this change needs, or runs something unrelated.

| steps (jobs) | Today's rules | Selector | Would skip | Would add |
|---|---|---|---|---|
| NVIDIA, CPU and others | 65 (85) | 60 (115) | 29 (33) | 24 (63) |
| AMD mirrors | 63 (88) | 48 (94) | 30 (39) | 15 (45) |

<details><summary>Selector would run (60)</summary>

- `arm-cpu-test` ×3
- `async-engine-inputs-utils-worker`
- `basic-correctness` ×2
- `basic-models-test-other-cpu`
- `basic-models-tests-extra-initialization` ×14
- `basic-models-tests-initialization`
- `basic-models-tests-other`
- `cpu-language-generation-and-pooling-model-tests` ×3
- `cpu-multi-modal-model-tests-n` ×4
- `cpu-multimodal-config`
- `cpu-params-env-tokenizers-parser`
- `cpu-qwen2-5-vl-multimodal-tests`
- `cpu-spec-decode-tests`
- `deepseek-v4-kernel-test-b200`
- `distributed-model-tests-2-gpus` ×3
- `engine`
- `entrypoints-integration-api-server` ×4
- `entrypoints-integration-api-server-generate`
- `entrypoints-integration-api-server-openai-completion`
- `examples`
- `fusion-e2e-config-sweep-h100`
- `fusion-e2e-quick-h100`
- `fusion-e2e-tp2-ar-rms-config-sweep-h100`
- `fusion-e2e-tp2-asynctp-config-sweep-h100`
- `fusion-e2e-tp2-b200`
- `fusion-e2e-tp2-quick-h100`
- `glm5next-unit-tests`
- `inkling-unit-tests-b200`
- `kernels-b200` ×3
- `kernels-helion-test` ×5
- `kernels-mamba-test`
- `kernels-root-misc-test-b200`
- `kimi-k3-unit-tests-b200`
- `language-models-tests-extra-standard` ×2
- `language-models-tests-granite-l4-compatibility`
- `language-models-tests-hybrid` ×2
- `language-models-tests-standard`
- `lora` ×4
- `lora-tp-distributed` ×4
- `model-executor`
- `multi-modal-models-standard-1-qwen2`
- `multi-modal-models-standard-2-qwen3-gemma`
- `multi-modal-models-standard-3-llava-qwen2-vl`
- `multi-modal-models-standard-4-other-whisper`
- `multi-modal-processor` ×4
- `multi-modal-processor-cpu` ×4
- `pipeline-context-parallelism-4-gpus`
- `pytorch-compilation-unit-tests`
- `quantization` ×4
- `quantized-models-test`
- `qwen4-exp-unit-tests`
- `qwen4-exp-unit-tests-cpu`
- `rayexecutorv2-4-gpus`
- `spec-decode-draft-model` ×4
- `v1-executor-worker`
- `v1-kv-connectors` ×4
- `v1-metrics-lmeval`
- `v1-others-cpu`
- `v1-sample`
- `v1-spec-decode`
</details>

<details><summary>Would skip (today's rules run them) (29)</summary>

- `ascend-npu-test`
- `basic-correctness-cpu-offload`
- `basic-correctness-cumem`
- `basic-correctness-prefetch-offload`
- `basic-correctness-sleep-mode`
- `benchmarks-cli-test`
- `cpu-reasoning-renderers`
- `cpu-tool-parsers`
- `entrypoints-integration-api-server-openai-chat_completion`
- `entrypoints-integration-llm`
- `entrypoints-integration-multimodal`
- `entrypoints-integration-pooling`
- `entrypoints-integration-responses-api`
- `entrypoints-integration-speech_to_text`
- `kernels-fla-ops-test-b200`
- `kernels-fusedmoe-layer-test-2-b200s`
- `kernels-fusedmoe-layer-test-2-h100s`
- `kernels-mhc-test-b200`
- `kernels-moe-test` ×5
- `metrics-tracing-2-gpus`
- `pytorch-compilation-dynamic-shapes`
- `pytorch-compilation-passes-unit-tests`
- `pytorch-compilation-unit-tests-h100`
- `pytorch-fullgraph-cudagraph-l4-compatibility`
- `pytorch-fullgraph-test`
- `regression`
- `v1-core`
- `v1-kv-offload`
- `v1-logits-oracle`
</details>

<details><summary>Would add (today's rules do not run them) (24)</summary>

- `arm-cpu-test` ×3 (code map)
- `basic-models-tests-extra-initialization` ×14 (code map)
- `cpu-multi-modal-model-tests-n` ×4 (code map)
- `cpu-qwen2-5-vl-multimodal-tests` (code map)
- `cpu-spec-decode-tests` (code map)
- `deepseek-v4-kernel-test-b200` (code map)
- `distributed-model-tests-2-gpus` ×3 (code map)
- `examples` (code map)
- `glm5next-unit-tests` (code map)
- `inkling-unit-tests-b200` (code map)
- `kernels-b200` ×3 (code map)
- `kernels-helion-test` ×5 (code map)
- `kernels-mamba-test` (code map)
- `kimi-k3-unit-tests-b200` (code map)
- `language-models-tests-extra-standard` ×2 (code map)
- `lora` ×4 (code map)
- `lora-tp-distributed` ×4 (code map)
- `pipeline-context-parallelism-4-gpus` (code map)
- `quantization` ×4 (code map)
- `quantized-models-test` (code map)
- `qwen4-exp-unit-tests` (code map)
- `qwen4-exp-unit-tests-cpu` (code map)
- `rayexecutorv2-4-gpus` (code map)
- `spec-decode-draft-model` ×4 (code map)
</details>

<details><summary>AMD mirrors: would skip (30)</summary>

- `basic-correctness-cpu-offload`
- `basic-correctness-cumem`
- `basic-correctness-prefetch-offload`
- `basic-correctness-sleep-mode`
- `benchmarks-cli-test`
- `entrypoints-integration-api-server-openai-chat_completion`
- `entrypoints-integration-llm`
- `entrypoints-integration-multimodal`
- `entrypoints-integration-pooling`
- `entrypoints-integration-responses-api`
- `entrypoints-integration-speech_to_text`
- `fault-tolerance-e2e-2xh100`
- `kernels-fla-ops-test-b200`
- `kernels-fp4-moe-test-b200`
- `kernels-fusedmoe-layer-test-2-b200s`
- `kernels-fusedmoe-layer-test-2-h100s`
- `kernels-mhc-test-b200`
- `kernels-moe-test` ×5
- `kernels-quantization-test` ×6
- `kernels-root-misc-test-b200`
- `metrics-tracing-2-gpus`
- `platform-tests`
- `pytorch-compilation-dynamic-shapes`
- `pytorch-compilation-passes-unit-tests`
- `pytorch-compilation-unit-tests-h100`
- `pytorch-fullgraph-test`
- `regression`
- `v1-core`
- `v1-kv-offload`
- `v1-logits-oracle`
</details>

<details><summary>AMD mirrors: would add (15)</summary>

- `basic-models-tests-extra-initialization` ×14 (code map)
- `examples` (code map)
- `glm5next-unit-tests` (code map)
- `inkling-unit-tests-b200` (code map)
- `kernels-helion-test` ×5 (code map)
- `kernels-mamba-test` (code map)
- `kimi-k3-unit-tests-b200` (code map)
- `language-models-tests-extra-standard` ×2 (code map)
- `lora` ×4 (code map)
- `lora-tp-distributed` ×4 (code map)
- `pipeline-context-parallelism-4-gpus` (code map)
- `quantization` ×4 (code map)
- `quantized-models-test` (code map)
- `qwen4-exp-unit-tests` (code map)
- `spec-decode-draft-model` ×4 (code map)
</details>

#### CI results (2026-09-30 17:02 UTC)

146 passed, 1 failed, 18 pending.
CI is still running; the picture below is not final.

No misses: every failed job the selector would skip was also failing on main.

- `computer-cpu-reasoning-plus-renderers`: selector would skip it; also failing on main (`cff08b461e`, `ff1b87cca2`, `73a5831127`), pre-existing

<sub>4 changed files · base `cff08b461e` · head `7aa30f8b04` · Python record: not used (/tmp/ci-infra-selector/buildkite/ci_selector/coverage-data/table.json.gz is table version 5, expected 7; re-merge it from the raw recordings) · kernel record: table 866fa130fa (build 92059), map 866fa130fa · not counted: 10 build steps, 5 A100 steps the generator no longer emits, 37 optional steps the selector would also run</sub>
