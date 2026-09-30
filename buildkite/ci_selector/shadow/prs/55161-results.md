<!-- ci-selector-shadow -->
### CI selector (shadow): 2 test steps (8 jobs) instead of 40 (62 jobs)

Shadow mode: this changes nothing about what CI runs. It shows what the evidence-based selector would pick for this PR, next to today's rules. [How it works](https://github.com/vllm-project/ci-infra/tree/main/buildkite/ci_selector).

**Feedback welcome:** reply here if it would skip a step this change needs, or runs something unrelated.

| steps (jobs) | Today's rules | Selector | Would skip | Would add |
|---|---|---|---|---|
| NVIDIA, CPU and others | 40 (62) | 2 (8) | 38 (54) | 0 (0) |
| AMD mirrors | 36 (56) | 2 (8) | 34 (48) | 0 (0) |

<details><summary>Selector would run (2)</summary>

- `lora` ×4
- `lora-tp-distributed` ×4
</details>

<details><summary>Would skip (today's rules run them) (38)</summary>

- `ascend-npu-test`
- `basic-correctness` ×2
- `basic-correctness-cpu-offload`
- `basic-correctness-cumem`
- `basic-correctness-prefetch-offload`
- `basic-correctness-sleep-mode`
- `basic-models-test-other-cpu`
- `basic-models-tests-initialization`
- `basic-models-tests-other`
- `benchmarks-cli-test`
- `cpu-language-generation-and-pooling-model-tests` ×3
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
- `kernels-root-misc-test-b200`
- `language-models-tests-granite-l4-compatibility`
- `language-models-tests-hybrid` ×2
- `language-models-tests-standard`
- `multi-modal-models-standard-1-qwen2`
- `multi-modal-models-standard-2-qwen3-gemma`
- `multi-modal-models-standard-3-llava-qwen2-vl`
- `multi-modal-models-standard-4-other-whisper`
- `multi-modal-processor` ×4
- `multi-modal-processor-cpu` ×4
- `v1-core`
- `v1-executor-worker`
- `v1-kv-connectors` ×4
- `v1-kv-offload`
- `v1-metrics-lmeval`
- `v1-others-cpu`
</details>

<details><summary>Would add (today's rules do not run them) (0)</summary>

none
</details>

<details><summary>AMD mirrors: would skip (34)</summary>

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
- `language-models-tests-granite-l4-compatibility`
- `language-models-tests-hybrid` ×2
- `language-models-tests-standard`
- `multi-modal-models-standard-1-qwen2`
- `multi-modal-models-standard-2-qwen3-gemma`
- `multi-modal-models-standard-3-llava-qwen2-vl`
- `multi-modal-models-standard-4-other-whisper`
- `multi-modal-processor` ×4
- `multi-modal-processor-cpu` ×4
- `platform-tests`
- `pytorch-compilation-passes-unit-tests`
- `v1-core`
- `v1-executor-worker`
- `v1-kv-connectors` ×4
- `v1-kv-offload`
- `v1-metrics-lmeval`
</details>

<details><summary>AMD mirrors: would add (0)</summary>

none
</details>

#### CI results (2026-09-30 04:26 UTC)

111 passed, 0 failed, 0 pending.

No failures to judge.

<sub>2 changed files · base `a9eafde59c` · head `0d448a441a` · Python record: build 91312 at `7871963fcc` · kernel record: table d882bddbea (build 91957), map d882bddbea · not counted: 10 build steps, 5 A100 steps the generator no longer emits</sub>
