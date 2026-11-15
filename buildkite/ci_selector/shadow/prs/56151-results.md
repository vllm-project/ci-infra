<!-- ci-selector-shadow -->
### CI selector (shadow): 29 test steps (67 jobs) instead of 34 (55 jobs)

Shadow mode: this changes nothing about what CI runs. It shows what the evidence-based selector would pick for this PR, next to today's rules. [How it works](https://github.com/vllm-project/ci-infra/tree/main/buildkite/ci_selector).

**Feedback welcome:** reply here if it would skip a step this change needs, or runs something unrelated.

| steps (jobs) | Today's rules | Selector | Would skip | Would add |
|---|---|---|---|---|
| NVIDIA, CPU and others | 34 (55) | 29 (67) | 18 (22) | 13 (34) |
| AMD mirrors | 34 (53) | 22 (51) | 21 (27) | 9 (25) |

<details><summary>Selector would run (29)</summary>

- `arm-cpu-test` ×3
- `basic-models-test-other-cpu`
- `basic-models-tests-extra-initialization` ×14
- `basic-models-tests-initialization`
- `basic-models-tests-other`
- `cpu-language-generation-and-pooling-model-tests` ×3
- `cpu-multi-modal-model-tests-n` ×4
- `cpu-qwen2-5-vl-multimodal-tests`
- `deepseek-v4-kernel-test-b200`
- `distributed-model-tests-2-gpus` ×3
- `glm5next-unit-tests`
- `inkling-unit-tests-b200`
- `kernels-attention-test` ×7
- `kernels-b200` ×3
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
</details>

<details><summary>Would skip (today's rules run them) (18)</summary>

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
- `kernels-fla-ops-test-b200`
- `kernels-mhc-test-b200`
</details>

<details><summary>Would add (today's rules do not run them) (13)</summary>

- `arm-cpu-test` ×3 (code map)
- `basic-models-tests-extra-initialization` ×14 (code map)
- `cpu-multi-modal-model-tests-n` ×4 (code map)
- `cpu-qwen2-5-vl-multimodal-tests` (code map)
- `deepseek-v4-kernel-test-b200` (code map)
- `distributed-model-tests-2-gpus` ×3 (code map)
- `glm5next-unit-tests` (code map)
- `inkling-unit-tests-b200` (code map)
- `kimi-k3-unit-tests-b200` (code map)
- `language-models-tests-extra-standard` ×2 (code map)
- `quantized-models-test` (code map)
- `qwen4-exp-unit-tests` (code map)
- `qwen4-exp-unit-tests-cpu` (code map)
</details>

<details><summary>AMD mirrors: would skip (21)</summary>

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
- `kernels-b200` ×3
- `kernels-fla-ops-test-b200`
- `kernels-mhc-test-b200`
- `kernels-root-misc-test-b200`
- `platform-tests`
- `pytorch-compilation-passes-unit-tests`
</details>

<details><summary>AMD mirrors: would add (9)</summary>

- `basic-models-tests-extra-initialization` ×14 (code map)
- `deepseek-v4-kernel-test-b200` (code map)
- `distributed-model-tests-2-gpus` ×3 (code map)
- `glm5next-unit-tests` (code map)
- `inkling-unit-tests-b200` (code map)
- `kimi-k3-unit-tests-b200` (code map)
- `language-models-tests-extra-standard` ×2 (code map)
- `quantized-models-test` (code map)
- `qwen4-exp-unit-tests` (code map)
</details>

#### CI results (2026-09-30 12:54 UTC)

100 passed, 0 failed, 0 pending.

No failures to judge.

<sub>2 changed files · base `741edeebee` · head `a7a47162f2` · Python record: not used (/tmp/ci-infra-selector/buildkite/ci_selector/coverage-data/table.json.gz is table version 5, expected 7; re-merge it from the raw recordings) · kernel record: table d882bddbea (build 91957), map d882bddbea · not counted: 10 build steps, 5 A100 steps the generator no longer emits, 18 optional steps the selector would also run</sub>
