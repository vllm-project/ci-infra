<!-- ci-selector-shadow -->
### CI selector (shadow): 29 test steps (66 jobs) instead of 32 (45 jobs)

Shadow mode: this changes nothing about what CI runs. It shows what the evidence-based selector would pick for this PR, next to today's rules. [How it works](https://github.com/vllm-project/ci-infra/tree/main/buildkite/ci_selector).

**Feedback welcome:** reply here if it would skip a step this change needs, or runs something unrelated.

| steps (jobs) | Today's rules | Selector | Would skip | Would add |
|---|---|---|---|---|
| NVIDIA, CPU and others | 32 (45) | 29 (66) | 20 (24) | 17 (45) |
| AMD mirrors | 32 (43) | 23 (53) | 21 (25) | 12 (35) |

<details><summary>Selector would run (29)</summary>

- `arm-cpu-test` ×3
- `basic-models-tests-extra-initialization` ×14
- `basic-models-tests-initialization`
- `basic-models-tests-other`
- `cpu-language-generation-and-pooling-model-tests` ×3
- `cpu-multi-modal-model-tests-n` ×4
- `cpu-multimodal-config`
- `cpu-reasoning-renderers`
- `distributed-model-tests-2-gpus` ×3
- `engine`
- `entrypoints-integration-speech_to_text`
- `kernels-core-operation-test` ×3
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
- `pytorch-compilation-unit-tests`
- `quantization` ×4
- `rayexecutorv2-4-gpus`
- `v1-attention-b200` ×2
- `v1-attention-h100-mi300` ×2
</details>

<details><summary>Would skip (today's rules run them) (20)</summary>

- `ascend-npu-test`
- `basic-correctness` ×2
- `basic-correctness-cpu-offload`
- `basic-correctness-cumem`
- `basic-correctness-prefetch-offload`
- `basic-correctness-sleep-mode`
- `basic-models-test-other-cpu`
- `benchmarks-cli-test`
- `entrypoints-integration-api-server` ×4
- `entrypoints-integration-api-server-generate`
- `entrypoints-integration-api-server-openai-chat_completion`
- `entrypoints-integration-api-server-openai-completion`
- `entrypoints-integration-llm`
- `entrypoints-integration-multimodal`
- `entrypoints-integration-pooling`
- `entrypoints-integration-responses-api`
- `kernels-fla-ops-test-b200`
- `kernels-mhc-test-b200`
- `multi-modal-models-standard-2-qwen3-gemma`
- `multi-modal-models-standard-3-llava-qwen2-vl`
</details>

<details><summary>Would add (today's rules do not run them) (17)</summary>

- `arm-cpu-test` ×3 (code map)
- `basic-models-tests-extra-initialization` ×14 (code map)
- `cpu-multi-modal-model-tests-n` ×4 (code map)
- `cpu-multimodal-config` (code map)
- `cpu-reasoning-renderers` (code map)
- `distributed-model-tests-2-gpus` ×3 (code map)
- `engine` (code map)
- `kernels-core-operation-test` ×3 (code map)
- `kimi-k3-unit-tests-b200` (code map)
- `language-models-tests-extra-standard` ×2 (code map)
- `model-executor` (code map)
- `pipeline-context-parallelism-4-gpus` (code map)
- `pytorch-compilation-unit-tests` (code map)
- `quantization` ×4 (code map)
- `rayexecutorv2-4-gpus` (code map)
- `v1-attention-b200` ×2 (code map)
- `v1-attention-h100-mi300` ×2 (code map)
</details>

<details><summary>AMD mirrors: would skip (21)</summary>

- `basic-correctness` ×2
- `basic-correctness-cpu-offload`
- `basic-correctness-cumem`
- `basic-correctness-prefetch-offload`
- `basic-correctness-sleep-mode`
- `basic-models-test-other-cpu`
- `benchmarks-cli-test`
- `entrypoints-integration-api-server` ×4
- `entrypoints-integration-api-server-generate`
- `entrypoints-integration-api-server-openai-chat_completion`
- `entrypoints-integration-api-server-openai-completion`
- `entrypoints-integration-llm`
- `entrypoints-integration-multimodal`
- `entrypoints-integration-pooling`
- `entrypoints-integration-responses-api`
- `kernels-fla-ops-test-b200`
- `kernels-mhc-test-b200`
- `multi-modal-models-standard-2-qwen3-gemma`
- `multi-modal-models-standard-3-llava-qwen2-vl`
- `platform-tests`
- `pytorch-compilation-passes-unit-tests`
</details>

<details><summary>AMD mirrors: would add (12)</summary>

- `basic-models-tests-extra-initialization` ×14 (code map)
- `distributed-model-tests-2-gpus` ×3 (code map)
- `engine` (code map)
- `fusion-e2e-tp2-asynctp-config-sweep-h100` (code map)
- `kernels-core-operation-test` ×3 (code map)
- `language-models-tests-extra-standard` ×2 (code map)
- `model-executor` (code map)
- `pipeline-context-parallelism-4-gpus` (code map)
- `pytorch-compilation-unit-tests` (code map)
- `quantization` ×4 (code map)
- `v1-attention-b200` ×2 (code map)
- `v1-attention-h100-mi300` ×2 (code map)
</details>

#### CI results (2026-09-30 17:09 UTC)

81 passed, 0 failed, 0 pending.

No failures to judge.

<sub>3 changed files · base `f4917dadc8` · head `8d834067e4` · Python record: not used (/tmp/ci-infra-selector/buildkite/ci_selector/coverage-data/table.json.gz is table version 5, expected 7; re-merge it from the raw recordings) · kernel record: table 866fa130fa (build 92059), map 866fa130fa · not counted: 10 build steps, 5 A100 steps the generator no longer emits, 14 optional steps the selector would also run</sub>
