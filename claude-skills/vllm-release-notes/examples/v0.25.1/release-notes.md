# vLLM v0.25.1

## Highlights

This release features 2 commits from 2 contributors (1 new)!

v0.25.1 is a patch release containing two targeted bug fixes on top of v0.25.0.

### Bug Fixes
* **Avoid blocking model launching when no system FFmpeg is available for TorchCodec** (#47888). Previously `import torchcodec` raised a `RuntimeError` at import time when system FFmpeg was missing, which blocked startup (e.g. `vllm serve Qwen/Qwen3-VL-2B-Instruct`) even when TorchCodec was not in use. The error is now deferred to runtime so it only surfaces if TorchCodec is actually needed.
* **Guard mixed-dtype allreduce RMSNorm quant fusions** (#48330). The fused FlashInfer allreduce + RMSNorm + static-quantization patterns could match graphs where the activation and RMSNorm weight dtypes differ (e.g. a BF16 residual stream with an FP32 Gemma/Qwen-style RMSNorm weight in NVFP4 models), corrupting the hidden state and producing garbage output such as repeated `!!!!!` tokens. A dtype-match guard now routes incompatible mixed-dtype graphs to the safe path, while same-dtype models retain the full allreduce + RMSNorm + quant fusion.

## Contributors

@Isotr0py, @hugo-cen

## New Contributors

* @hugo-cen made their first contribution in https://github.com/vllm-project/vllm/pull/48330
