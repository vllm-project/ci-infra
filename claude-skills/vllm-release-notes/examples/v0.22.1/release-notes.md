## Highlights

This release features 8 commits from 6 contributors (1 new)!

v0.22.1 is a patch release on top of v0.22.0 with targeted bug fixes plus a couple of additions: new model support for JetBrains' Mellum v2, zentorch-accelerated quantized linear inference on AMD Zen CPUs, and fixes for multi-node Ray data-parallel serving, DeepSeek-V4 initialization, and a few model-loading regressions.

### Model Support
* New model: JetBrains' **Mellum v2**, an open-weights Mixture-of-Experts code-generation model (#43992).
* **DeepSeek-V4**: resolve a CUTLASS `fmin` compatibility issue that broke initialization (0decac0d).
* Fix `OlmoHybridForCausalLM` failing to initialise after the checkpoint changed `rope_parameters` from `None` to `{"rope_type": None}` (#43846).
* Fix **HyperCLOVAX** loading after the upstream HuggingFace repo removed its remote code (now native in `transformers >= 5.9.0`): register the `hyperclovax` model_type so vLLM uses its vendored config instead of the stale `auto_map` (#43860).

### Hardware & Performance
* **AMD Zen CPUs**: route W8A8 (int8 dynamic-symmetric) and W4A16 (GPTQ) linear inference through zentorch kernels, registered ahead of the generic oneDNN CPU kernels, with transparent fallback on non-Zen CPUs, GPUs, and XPU (#41813).

### Large Scale Serving
* Fix a deterministic hang in multi-node **Ray data-parallel** serving with `num_api_servers > 1` by excluding the Ray DP backend from the deferred (kernel-assigned) port allocation introduced in #42585 (#43864).

### Build & CI
* Docker: stop installing `flashinfer-jit-cache` via `--extra-index-url` while it is quarantined on PyPI, fixing image builds (#44366).
* Normalize **NIXL** KV-connector wheel installs so only the wheel matching the image's CUDA major is kept, fixing `ImportError: libcudart.so.12` when importing `nixl_ep` on CUDA 13 images (#44266).

## Contributors

@khluu, @vadiklyutiy, @aadwived, @shadeMe, @alec-flowers, @hmellor

## New Contributors

* @aadwived made their first contribution in https://github.com/vllm-project/vllm/pull/41813
