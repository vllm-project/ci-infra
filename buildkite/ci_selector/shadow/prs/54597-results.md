<!-- ci-selector-shadow -->
### CI selector (shadow): 47 test steps (100 jobs) instead of 51 (87 jobs)

Shadow mode: this changes nothing about what CI runs. It shows what the evidence-based selector would pick for this PR, next to today's rules. [How it works](https://github.com/vllm-project/ci-infra/tree/main/buildkite/ci_selector).

**Feedback welcome:** reply here if it would skip a step this change needs, or runs something unrelated.

| steps (jobs) | Today's rules | Selector | Would skip | Would add |
|---|---|---|---|---|
| NVIDIA, CPU and others | 51 (87) | 47 (100) | 16 (20) | 12 (33) |
| AMD mirrors | 48 (82) | 39 (85) | 17 (21) | 8 (24) |

<details><summary>Selector would run (47)</summary>

- `amd-fp8-moe-kernels-mi355`
- `amd-kernels-mi355`
- `amd-native-quantization-kernels-mi355`
- `arm-cpu-test` ×3
- `basic-models-test-other-cpu`
- `basic-models-tests-extra-initialization` ×14
- `basic-models-tests-initialization`
- `basic-models-tests-other`
- `cpu-language-generation-and-pooling-model-tests` ×3
- `cpu-multi-modal-model-tests-n` ×4
- `cpu-qwen2-5-vl-multimodal-tests`
- `deepseek-v4-kernel-test-b200`
- `deepseek-v4-kernel-test-h100`
- `distributed-model-tests-2-gpus` ×3
- `glm5next-unit-tests`
- `inkling-unit-tests-b200`
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
- `language-models-tests-extra-standard` ×2
- `language-models-tests-granite-l4-compatibility`
- `language-models-tests-hybrid` ×2
- `language-models-tests-standard`
- `multi-modal-models-standard-1-qwen2`
- `multi-modal-models-standard-2-qwen3-gemma`
- `multi-modal-models-standard-3-llava-qwen2-vl`
- `multi-modal-models-standard-4-other-whisper`
- `multi-modal-processor` ×4
- `multi-modal-processor-cpu` ×4
- `quantized-models-test`
- `qwen4-exp-unit-tests`
- `qwen4-exp-unit-tests-cpu`
- `vllm-ir-tests`
</details>

<details><summary>Would skip (today's rules run them) (16)</summary>

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
- `entrypoints-integration-pooling`
- `entrypoints-integration-responses-api`
- `entrypoints-integration-speech_to_text`
</details>

<details><summary>Would add (today's rules do not run them) (12)</summary>

- `arm-cpu-test` ×3 (code map)
- `basic-models-tests-extra-initialization` ×14 (code map)
- `cpu-multi-modal-model-tests-n` ×4 (code map)
- `cpu-qwen2-5-vl-multimodal-tests` (code map)
- `distributed-model-tests-2-gpus` ×3 (code map)
- `glm5next-unit-tests` (code map)
- `inkling-unit-tests-b200` (code map)
- `kimi-k3-unit-tests-b200` (code map)
- `language-models-tests-extra-standard` ×2 (code map)
- `quantized-models-test` (code map)
- `qwen4-exp-unit-tests` (code map)
- `qwen4-exp-unit-tests-cpu` (code map)
</details>

<details><summary>AMD mirrors: would skip (17)</summary>

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
- `entrypoints-integration-pooling`
- `entrypoints-integration-responses-api`
- `entrypoints-integration-speech_to_text`
- `platform-tests`
- `pytorch-compilation-passes-unit-tests`
</details>

<details><summary>AMD mirrors: would add (8)</summary>

- `basic-models-tests-extra-initialization` ×14 (code map)
- `distributed-model-tests-2-gpus` ×3 (code map)
- `glm5next-unit-tests` (code map)
- `inkling-unit-tests-b200` (code map)
- `kimi-k3-unit-tests-b200` (code map)
- `language-models-tests-extra-standard` ×2 (code map)
- `quantized-models-test` (code map)
- `qwen4-exp-unit-tests` (code map)
</details>

#### CI results (2026-09-30 17:00 UTC)

153 passed, 1 failed, 3 pending.
CI is still running; the picture below is not final.

**1 possible miss(es):** failed here, the selector would have skipped them, and main was not failing them.

- `amd-mi300-basic-correctness-sleep-mode`: **selector would skip it; not failing on main**

<sub>3 changed files · base `cff08b461e` · head `1e2fc48cc5` · Python record: not used (/tmp/ci-infra-selector/buildkite/ci_selector/coverage-data/table.json.gz is table version 5, expected 7; re-merge it from the raw recordings) · kernel record: table 866fa130fa (build 92059), map 866fa130fa · not counted: 10 build steps, 5 A100 steps the generator no longer emits, 23 optional steps the selector would also run</sub>
