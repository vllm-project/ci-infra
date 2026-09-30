<!-- ci-selector-shadow -->
### CI selector (shadow): 11 test steps (14 jobs) instead of 36 (49 jobs)

Shadow mode: this changes nothing about what CI runs. It shows what the evidence-based selector would pick for this PR, next to today's rules. [How it works](https://github.com/vllm-project/ci-infra/tree/main/buildkite/ci_selector).

**Feedback welcome:** reply here if it would skip a step this change needs, or runs something unrelated.

| steps (jobs) | Today's rules | Selector | Would skip | Would add |
|---|---|---|---|---|
| NVIDIA, CPU and others | 36 (49) | 11 (14) | 27 (37) | 2 (2) |
| AMD mirrors | 30 (41) | 8 (11) | 24 (32) | 2 (2) |

<details><summary>Selector would run (11)</summary>

- `cpu-params-env-tokenizers-parser`
- `cpu-reasoning-renderers`
- `cpu-tool-parsers`
- `entrypoints-integration-api-server` ×4
- `entrypoints-integration-api-server-generate`
- `entrypoints-integration-api-server-openai-chat_completion`
- `entrypoints-integration-api-server-openai-completion`
- `entrypoints-integration-responses-api`
- `entrypoints-unit-tests`
- `mrcr-eval-small-models`
- `rust-frontend-tool-use`
</details>

<details><summary>Would skip (today's rules run them) (27)</summary>

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
- `cpu-multimodal-config`
- `entrypoints-integration-llm`
- `entrypoints-integration-multimodal`
- `entrypoints-integration-pooling`
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
</details>

<details><summary>Would add (today's rules do not run them) (2)</summary>

- `entrypoints-unit-tests` (code map)
- `mrcr-eval-small-models` (Python record)
</details>

<details><summary>AMD mirrors: would skip (24)</summary>

- `basic-correctness` ×2
- `basic-correctness-cpu-offload`
- `basic-correctness-cumem`
- `basic-correctness-prefetch-offload`
- `basic-correctness-sleep-mode`
- `basic-models-test-other-cpu`
- `basic-models-tests-initialization`
- `basic-models-tests-other`
- `benchmarks-cli-test`
- `entrypoints-integration-llm`
- `entrypoints-integration-multimodal`
- `entrypoints-integration-pooling`
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
</details>

<details><summary>AMD mirrors: would add (2)</summary>

- `entrypoints-unit-tests` (code map)
- `mrcr-eval-small-models` (Python record)
</details>

#### CI results (2026-09-30 04:23 UTC)

83 passed, 0 failed, 0 pending.

No failures to judge.

<sub>6 changed files · base `0376f81530` · head `2b9b55c7f1` · Python record: build 91312 at `7871963fcc` · kernel record: table d882bddbea (build 91957), map d882bddbea · not counted: 10 build steps, 5 A100 steps the generator no longer emits</sub>
