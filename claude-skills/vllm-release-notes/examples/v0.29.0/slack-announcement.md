# v0.29.0 Slack announcement

## Main message

Hi all,
We just finished vLLM v0.29.0 release today. :rocket: This release features 594 commits from 277 contributors. We also welcomed 91 new contributors to vLLM!
A few highlights of this release: Model Runner V2 is now the default for all models, new models (Hy4-preview, Qwen3.8-Flash-Next), Kimi K3 and DeepSeek V4 performance push, new RL weight sync backends, and more!
More details and install instructions in :thread:

Thank you everyone for your contributions to the new version of :vllm: :pray:

## Thread reply (install instructions)

• Python wheels:
    ◦ PyPI (CUDA 13.0):
        ▪ Install with `pip install vllm`
        ▪ (Preferred) `uv pip install vllm --torch-backend=auto`
    ◦ ROCm: `pip install vllm --extra-index-url https://wheels.vllm.ai/rocm/0.29.0/rocm723`
    ◦ XPU: `uv pip install vllm --extra-index-url https://wheels.vllm.ai/0.29.0/xpu --extra-index-url https://download.pytorch.org/whl/xpu --index-strategy unsafe-best-match`
• Docker images (`latest` tag also works in place of every `v0.29.0` tag here):
    ◦ CUDA 13.0 (Default) `docker pull vllm/vllm-openai:v0.29.0` (`v0.29.0-cu130` also works)
    ◦ CUDA 12.9: `docker pull vllm/vllm-openai:v0.29.0-cu129`
    ◦ CUDA 13.0 Ubuntu 24.04: `docker pull vllm/vllm-openai:v0.29.0-ubuntu2404`
    ◦ CUDA 12.9 Ubuntu 24.04: `docker pull vllm/vllm-openai:v0.29.0-cu129-ubuntu2404`
    ◦ ROCm: `docker pull vllm/vllm-openai-rocm:v0.29.0`
    ◦ CPU: `docker pull vllm/vllm-openai-cpu:v0.29.0`
    ◦ XPU: `docker pull vllm/vllm-openai-xpu:v0.29.0`
• Github Release: https://github.com/vllm-project/vllm/releases/tag/v0.29.0
    ◦ Source distribution tarball
    ◦ CUDA 12.9 Python wheels for x86_64 and arm64
    ◦ CUDA 13.0 Python wheels for x86_64 and arm64
    ◦ CPU Python wheel for x86_64, arm64, and MacOS
    ◦ XPU Python wheel for x86_64

## Thread reply (heads-up on breaking changes)

:warning: A few breaking changes to be aware of in v0.29.0:
• Model Runner V2 is now the default runner for all models (MRV1 still used for a few ROCm models and unsupported features).
• Ten deprecated model architectures were removed (Arctic, Chameleon, Cheers, Fairseq2Llama, FireRedLID, GritLM, HCXVision, MPT, RW/StableLMEpoch aliases, PrithviGeoSpatialMAE). FlexOlmo, Olmo3 and Hunyuan V1/VL now run via the Transformers modeling backend.
• PyAV video decoder backend removed; use OpenCV or Torchcodec.
• `python -m vllm.entrypoints.openai.api_server` is deprecated; use `vllm serve`.
• `prefix_cache_retention_interval` is now a CLI arg and defaults to 0 for SWA/SSM models (dense retention kept automatically for hybrid models with EAGLE/MTP).
Full list in the "Breaking Changes & Deprecations" section of the release notes.
