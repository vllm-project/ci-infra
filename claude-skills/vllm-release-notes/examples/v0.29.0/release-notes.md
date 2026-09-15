# v0.29.0

## Highlights

This release features 594 commits from 277 contributors (91 new)!

* **Model Runner V2 is now the default for all models** (#53183), completing the rollout that began with pooling models (#48290). MRV2 also gained CUDA graph memory profiling for KV cache auto-sizing (#53306), batch-sharded sampling that cuts per-step logits memory by 1/TP (#50465), prompt embeds (#42963), `extract_hidden_states` speculation (#49811), padded FULL cudagraph dispatch for uniform decode under spec decode (#53407), and DP-sync skipping before EAGLE/MTP draft prefill (#53694). MRV1 remains in use for a few ROCm models and features MRV2 does not yet support.
* **New models**: Hy4-preview, Tencent's 770B/49B-active MoE with Gated DeepSeek Sparse Attention and native MTP (#54160); Qwen3.8-Flash-Next with BF16/FP8/NVFP4 and MTP (#53896); GraniteSWA and GraniteMoeSWA (#52706); NemotronH_Omni_Reasoning_V3 with MTP (#52929, #53121); Kimi K3 NVFP4 checkpoints (#53132).
* **Kimi-K3 and DeepSeek V4 performance**: fused MXFP4 top-k finalization in the K3 latent tail (about 5% E2E latency, #53152), K3 Mamba metadata preparation in one Triton launch (6.6-7.6x kernel speedup, #52388), tuned Hopper low-latency GEMM (#54088) now also dispatched on SM100 (#53534) and used for `eh_proj` (12.9-25.2% kernel speedup, #53942), GEMM-RS extended to GEMM-AR (#53053), MLA gate merged into the QKV-A projection (#54015), K3 DCP with DSpark (#52188) and DCP partial prefix cache hits (#50493); DeepSeek V4 shared experts fused into MegaMoE (#53040), adaptive top-k width re-landed (#52823), a native SwiGLU clamp kernel for Humming MoE (#53685), and an opt-in FlashInfer `moe_ep` expert backend (#49636).
* **Speculative decoding**: per-request acceptance stats in OpenAI API responses via `--per-request-spec-decode-metrics` (#48915), adaptive verification extended to logprobs (#52242), SM100 sparse MLA for GLM-5.2 (#52783) and DeepSeek V4 on SM90 (#52795), Qwen3-Omni DSpark drafts (#52560), PLaMo3 EAGLE-3/DFlash (#54239), and DFlash2 loading from speculators format (#53797).
* **RL weight sync**: a new `sharded_rdt` P2P backend where each worker pulls only its TP/EP slice over NIXL or Ray Direct Transport (#43375), rank-local IPC weight updates (#52497), sparse checkpoint-coordinate updates through native weight loaders (#50723, #53751), and routed expert loading for gpt-oss (#52209).
* **Mamba prefix caching**: internal prefill checkpoints deliver a 9%-25% TTFT improvement (#52789); `prefix_cache_retention_interval` is now a CLI argument defaulting to 0 (#52216), with dense retention automatically restored for hybrid models using EAGLE/MTP (#55760, #55861).
* **New defaults**: FlashInfer all-reduce enabled by default for TP CUDA groups, opt out with `VLLM_ALLREDUCE_USE_FLASHINFER=0` (#52998); prefix-cache `NONE_HASH` is deterministic by default so distributed KV cache users no longer need to pin `PYTHONHASHSEED` (#51875); new `--max-num-queued-reqs` / `--max-num-queued-tokens` admission-control flags (#49445).
* **Breaking changes**: ten deprecated model architectures removed (#53608); FlexOlmo, Olmo3 and Hunyuan V1/VL migrated to the Transformers modeling backend (#53615); PyAV video decoder backend removed (#54231); `python -m vllm.entrypoints.openai.api_server` deprecated in favor of `vllm serve` (#52131); `VLLM_TEST_FORCE_FP8_MARLIN` (#52182) and `VLLM_ROCM_USE_AITER_FP4_ASM_GEMM` (#53141) removed.

## Release Artifacts

### Python Wheels

| Platform | Install |
|---|---|
| PyPI (CUDA 13.0) | `pip install vllm` |
| PyPI (CUDA 13.0, uv) | `uv pip install vllm --torch-backend=auto` |
| ROCm | `pip install vllm --extra-index-url https://wheels.vllm.ai/rocm/0.29.0/rocm723` |
| XPU | `uv pip install vllm --extra-index-url https://wheels.vllm.ai/0.29.0/xpu --extra-index-url https://download.pytorch.org/whl/xpu --index-strategy unsafe-best-match` |

### Docker Images

| Platform | Docker Image |
|---|---|
| CUDA 13.0 (Default) | `docker pull vllm/vllm-openai:v0.29.0` (`v0.29.0-cu130` also works) |
| CUDA 12.9 | `docker pull vllm/vllm-openai:v0.29.0-cu129` |
| CUDA 13.0 + Ubuntu 24.04 | `docker pull vllm/vllm-openai:v0.29.0-ubuntu2404` |
| CUDA 12.9 + Ubuntu 24.04 | `docker pull vllm/vllm-openai:v0.29.0-cu129-ubuntu2404` |
| ROCm | `docker pull vllm/vllm-openai-rocm:v0.29.0` |
| CPU | `docker pull vllm/vllm-openai-cpu:v0.29.0` |
| XPU | `docker pull vllm/vllm-openai-xpu:v0.29.0` |

### Other Artifacts

Pre-built release artifacts are available in the **Assets** section at the bottom of this page, including:
- Source distribution tarball
- CUDA 12.9 Python wheels for x86_64 and arm64
- CUDA 13.0 Python wheels for x86_64 and arm64
- CPU Python wheels for x86_64, arm64, and macOS
- XPU Python wheel for x86_64

## Model Support
* **New models**: Hy4-preview (#54160), Qwen3.8-Flash-Next (#53896), GraniteSWA and GraniteMoeSWA via the existing Granite implementation (#52706), NemotronH_Omni_Reasoning_V3 (#52929) with MTP for Nemotron VL models (#53121), bidirectional attention for DeepSeek-backbone embedding models (#52948), FP8 ModernBERT (#53101), and Kimi K3 NVFP4 checkpoints (#53132).
* **Transformers modeling backend**: FlexOlmo, Olmo3 and Hunyuan V1/VL migrated off native implementations (#53615), multimodal path hardened (#51827), and `RMSNormFuser.fuse` performance fixed (#52766).
* **Weight loading**: weight tying now inspects the checkpoint so a real `lm_head` is loaded even when the config claims tied embeddings (#51665, #53170).
* **LoRA**: DeepSeek V4 (#53361), Qwen3-Omni multimodal LoRA (#52786, #53557), tower/connector LoRA for LLaVA-NeXT (#49788) and LFM2-VL (#51498), plus fixes for Muse-Glimmer (#53513), Qwen3.5 embedding modules (#48850), partial LoRA on Qwen3.5/3.6 GatedDeltaNet (#47640), int32 overflow in punica kernels at long context (#53034), false target matches on unsupported module types (#52313), and base-layer/routed-expert prefix ordering (#52552).
* **Multimodal performance**: ViT full CUDA graph for Idefics3 and SmolVLM (#47625), packed encoder attention for Pixtral (#52185), fused Kimi vision Q/K RoPE kernel (#50400), Dots3 NOTE runtime (#53517) and Omni encoder optimizations (#53460), MM tensors no longer broadcast to workers for prefix-cache-covered items (#52041), redundant placeholder scans skipped (#52925), common token sequences cached (#53560), async media resolved concurrently across modalities (#54537), and H2D copies pinned to avoid stream stalls (#53412, #54292, #54299).
* **Multimodal inputs**: video embeds accepted by the Python frontend (#54242), modality-scoped `mm_processor_kwargs` honored (#53808), Qwen3-VL profiling honors `cap_pixels_per_frame` (#54380), video frame sampling respects MP4 edit-list trims (#48608), oversized items skip the MM processor cache instead of crashing (#53016), encoder cache entries survive until their last use (#54284), and repeated multimodal requests with the SHM cache no longer terminate the engine (#54994).
* **Correctness**: PaliGemma stale image scaling removed (#52692), Mistral3 placeholder grid with processor size overrides (#52874), MiniCPM-o on Transformers v5 (#54501), JinaVL cache order (#53553), mixed CLIP/SigLIP pooling batches (#53165), XD-RoPE models on prefix-cache hits (#53456), Qwen3-Omni audio encoder with non-divisible TP (#50858), GLM-5.2 no longer uses dense MHA (#52512), Moondream3 MoE all-reduce (#54152), deepseek-vl2 config defaults (#51302), HY-V3 compressed-tensors ignore matching (#48682), MiniMax-M3 FP8 query allocation under CUDA graph replay (#51203), and GraniteMoeHybrid quantized expert loading (#54052).

## Engine Core
* **Model Runner V2**: default for all models (#53183) and pooling models (#48290), CUDA graph memory reservation (#53306), batch-sharded sampling (#50465), prompt embeds (#42963), `extract_hidden_states` (#49811), padded FULL cudagraph dispatch (#53407), DP-sync skipping for drafts (#53694), decoupled draft/target gumbel noise streams (#54282), encoder-only path split out (#53176), and memory released correctly on shutdown and sleep (#53508, #54246, #54162, #53955, #53682).
* **Speculative decoding**: per-request acceptance stats (#48915), adaptive verification with logprobs (#52242), on SM100 sparse MLA (#52783) and DSv4 + SM90 (#52795), varlen trtllm-gen decode (#52157), FlashInfer MLA for DSpark drafting under DCP (#54277), fused GDN MTP for all Qwen head ratios (#52539), widest uniform decode batch captured by default (#50488) with memory-safe graph sizes (#54418), and fixes for short_conv/LFM2 targets (#50272), speculators-format config overrides (#42376), DFlash draft RoPE layout (#54373), draft models with a different GQA ratio (#53002), draft models with a larger hidden size under TP (#52193), DSpark backend inheritance scoped to DeepSeek V4 (#52809), generic `DSparkDraftModel` configs for Qwen3 (#52197), and Gemma4 MTP under CUDA graphs (#53884).
* **Prefix caching & scheduling**: Mamba internal prefill checkpoints (#52789), `prefix_cache_retention_interval` argument (#52216, #55760, #55861), deterministic `NONE_HASH` (#51875), queue admission control (#49445), KV null block reserved when validating `max_model_len` (#47272), spec decode no longer padded up to `max_model_len` (#53962), and negative external block allocation prevented (#52707).
* **RL workflows**: `sharded_rdt` P2P weight sync (#43375), rank-local IPC updates (#52497), sparse checkpoint updates (#50723, #53751), gpt-oss routed expert loading (#52209), stable DeepSeek V4 mHC broadcast buffers across weight sync (#52626), and packed weight transfer stream reuse capping reserved-memory waste (#52951).
* **Determinism**: `trace_decode_token_ids` for deterministic decode replay (#46701), per-arch tuned batch-invariant matmul configs (about 3x decode kernels on RTX 4090D/H20, #53247), Blackwell autotuning with 33.6% E2E latency reduction (#53649), deterministic MoE combine under DP+EP (#45683), and `fuse_allreduce_rms` disabled under batch invariance (#51292).
* **Kernels**: fused embedding kernel (#53677), FA4 re-enabled for head_size=256 on Blackwell (#52980), vectorized sparse MLA mask loads (#52217), masked MHA prefill for GLM-5 head dimensions (#53785), GLM-5.2 sparse MLA Q concatenation fused with head padding (#53878), fused QK-norm + partial MRoPE + gate for Qwen3.6 (#52676), FlashInfer CuTeDSL BF16 low-latency GEMM as opt-in `--linear-backend flashinfer_cutedsl` (#50572), replicated embedding and norm fusion for DSV3 flat models (#48484), standardized fused shared-expert selection (#51695), GPT-OSS MoE topk metadata reuse (#45457), tuned cooperative topk (#53382), and tuned FP8 fused_moe for Qwen3.5 on L40S (+7%, #53819).
* **Robustness**: KV cache layout standardized under a `KVCacheLayout` enum (#51718), JIT warmup provider registry (#50174), `--cpu-offload-params` now reaches vision/audio towers (#53120), attention backend probe failures no longer crash init (#51703), FlashInfer XQA falls back on unsupported head_dim (#53111), FlashInfer prefill LSE normalized before merging to fix prefix-cache logits divergence (#52796), seed preserved when a batch mixes seeded and unseeded requests (#51866), startup thread allocation accounts for local DP workers (#52385), int32 overflow fixes in fused SiLU block quant (#53409) and LoRA kernels (#53034), a shared-memory race in fused groupwise RMSNorm quantization (#54111), BLHNC addressing for FlashInfer sparse MLA (#54465), Mamba state copy race (#50729), and a `start_profile` no-op after auto-stop (#51839).

## Hardware & Performance
* **NVIDIA**: DeepSeek V3.2 / GLM-5.2 DSA routed to the optimized CUDA path on all GPUs (#52861), PCP for DSv3.2 sparse MLA (#52046), cuBLAS out_dtype router GEMM on all archs including GB10 (#54048), FlashInfer all-reduce tuning on SM103 (#53318, #53606), FA4 hdim256 on SM100 (#52980), SM120 sparse MLA fixes (#51395, #53574), DeepSeek V3.2 fused kernel grids hardened for 65k+ token launches (#52381), FlashMLA sparse decode workspace fix (#53755), MNNVL Lamport corruption fix (#53000), and opt-in Rubin Docker builds for CUDA 13.4/13.5 (#53443).
* **AMD ROCm**: dual-stream decode with hipgraphs (#52033), W4A4 preshuffled asm GEMM by default (+15% throughput on Llama-3.3-70B MXFP4, #53141), ROCr/CLR update fixing graph replay segfaults with up to about 20% TPOT improvement (#53712), fused KDA decode on MI325X (#52293), FULL cudagraphs for AITER MLA spec decode (#51171), FP8 asm MLA prefill for non-divisor head counts (#51040), DCP causal multi-token verification (#51705) and prefix cache hits for Kimi-K3 (#53598), AITER PA gluon decode for MiniMax-M3 (#52849), DeepSeek-V4 fusions for mHC/RMSNorm (#52737), C4A top-k (#52882), C4 compressor GEMMs (#53838) and SWA q/kv norm + FP8 quant (#53540), fused shared experts for block-FP8 (#53097), CPU offload on ROCm 7.13+ (#43018), TheRock 7.14 preview docker (#49925), int4/int8 quantization fixes (#52112, #48998, #51632, #53110), CUDA graphs captured on the current stream (#53818), AITER metadata preserved across graph replay (#53821), and improved ROCm detection under WSL (#38434).
* **Intel XPU**: INC int4 W4A8 linear backend (#50501), AutoRound MXFP8 MoE (#51248), EC connector KV offloading (#49532), and fixes for HunyuanOCR XD-RoPE (#52174), sparse-MLA metadata sync (#52066), Mamba state pointer overflow (#48109), oneCCL warm-up at world size 1 (#52389), and MRoPE (#53201).
* **CPU**: AMX high-performance MLA backend for DeepSeek V2/V3/R1 (#52616) with MLA now running end-to-end (#51471), FP16/BF16 persisted GDN state on AMX (#52191), Int8 MoE through zentorch on AMD Zen (#44834), Voxtral support (#53921), C++ causal_conv1d GDN on non-AMX AVX-512BF16 (#49688), and assorted fixes (#54042).

## Large Scale Serving
* **Context parallelism**: Kimi-K3 DCP with DSpark (#52188) and partial prefix cache hits (#50493), FlashInfer native CP for MLA decode (#54012), FlashMLA sparse DCP on Hopper with MTP (#46514), NIXL P/D DCP for MLA models (#50611), PCP for DSv3.2 (#52046) and NIXL PCP producers (#52779), `--dcp-q-replicate` with query replication default-on for GLM sparse attention (#50382), DCP fused attention fix for DeepSeek-V3.2 / GLM-5.2 (#50005), sparse MLA metadata and kernel block sizes under DCP (#52377, #51031), PCP PIECEWISE cudagraph fixes (#53869, #53515), and PCP compatibility checks delegated to the PCP manager so plugins can enable GQA+PCP (#53853).
* **Elastic EP**: reduced eager-mode reconfiguration downtime (#51885), AOT cache reuse preserved during scaling (#53378), and scale-below-minimum rejected (#52702).
* **MoE communication**: DeepEP v2 receiver CPU overhead (#51114) and MXFP8 activation scale dispatch (#51398), FlashInfer one-sided All2All refinements (#51924), DeepEP v2 fixes for `--enforce-eager` startup (#51824) and the decode/cudagraph path (#52632), NCCL>=2.31 heap overflow fix (#53008), cross-node MNNVL all-reduce gated by capability (#53253), and MNNVL all-reduce buffers sized for DSpark (#50932).
* **KV connectors**: Mooncake Store decode KV saving via `save_decode_cache` (#52466) and hybrid DCP prefix caching (#53324), externally transferable KV cache group identification (#53779), async KV loads deferred past forward launch (#53333), `kv_transfer_params` for `/inference/v1/generate` (#42644), MoRIIO shared KV region registration after the layout refactor (#53698), and Mamba fixes for Mooncake (#51362, #51358, #53663) and NIXL (#53523).
* **KV offloading**: EC offloading connector driven by CUDA events (#49994), ownership in KV cache events (#52067, #52068), P2P tier request-level offload (#52912) and abort handling (#52571), `/dev/shm` leak on crash fixed (#52596), CPU->GPU loads ordered against compute stream (#50696), `store_threshold` counting fixed (#52227), in-flight primary keys cascaded (#53329), and padded GPU cache storage handled (#54021).
* **Data/pipeline parallel**: PP silent corruption fix (#54962, #49274), DP coordinator wake handling (#51481), device sync on pause (#52914), TCPStore port fixes for Ray (#53666, #50969), DP supervisor inheriting the uvicorn config (#52473), EPD encoder round-robin fix (#52491), and producer-only EC config normalization (#53656).

## Quantization
* **New backends**: FlashInfer TRT-LLM MXFP8 linear (#52204), b12x FP4 MoE for SM120/SM121 (#52018), AutoRound block-wise FP8 (#47434), Humming MoE with MXFP4 weights + block-FP8 activations (#51332), and Humming for compressed-tensors WNA16 MoE (#48918).
* **Fixes**: weight-only NVFP4 checkpoints routed through W4A16 (#54427), compressed-tensors block FP8 with Marlin (#52966) and int8 grouped WNA16 MoE (#52002), OCP MX mxfp6 activation emulation (#52704), MXFP8 FlashInfer path guarded on availability (#52648), and Humming activation aliasing (#54056).

## API & Frontend
* **New endpoints & options**: `/v1/messages/render` for the Anthropic Messages API (#45803), `/cohere/v2/chat/render` (#53219), SSE keep-alive comments for idle streams (#51034), per-request spec decode metrics (#48915), video embeds input (#54242), `--max-num-queued-reqs` / `--max-num-queued-tokens` (#49445), and pooling requests can set padding (#51157).
* **Anthropic & Cohere**: `vllm_xargs` forwarded to sampling params (#53308), `stop_sequence` stop reason reported (#45807), Cohere citation/tool 500s fixed (#52175), and stop string limit applied (#53750).
* **OpenAI compatibility**: `logprobs=-1` in Completions (#46175), streamed logprob offsets with echo (#47815), batched chat echo (#52529), all choices returned from `/inference/v1/generate` with n>1 (#52399), `cache_salt` forwarded for content parts (#54315), `stop_token_ids` validated against vocab (#54196), and 4xx for client errors in `/detokenize` (#52622), malformed namespace tools (#53763), malformed base64 audio (#53744), and unknown chat roles in DeepSeek encoders (#53071); tool-call arguments in replayed history are parsed defensively (#48922), empty `FlatLogprobs` slices are handled (#53704), and empty bad-word tokenizations are rejected (#53433).
* **Structured output & parsers**: reasoning-end detection scoped to the current turn (#54089), terminal grammars stop under `min_tokens` (#54218), unsupported pattern+length schemas rejected (#49996), MistralCommonBackend tokenizers (#52720), XGrammar termination in batches (#52805), spurious FSM errors after speculative reasoning end (#53046), shared parser engine adapters (#52830), Gemma4 parenthesized tool calls (#53657) and `enable_thinking` default (#52430), HY-V3 parallel calls in one delta (#53965), GPT-OSS Harmony strict grammar (#52222), Kimi K3 reserved markers excluded from response text (#52889), and unused request-local reasoners skipped (#52573).
* **Rust frontend**: HY3 unified parser with local XGrammar structural tags (#53054), gRPC audio/video inputs (#53760), gRPC LoRA lifecycle control (#52840, #52031, #53756), `--generation-config vllm` (#53044), `truncate_prompt_tokens` (#48584), OpenAI edge-case alignment (#53218), optimized SSE hot path (#51321), pure-Rust `protox` replacing `protoc` (#52892), RL world-size reporting (#53204) and routed expert prompt offsets (#52703), and fixes for GLM-5.2 template parity (#51426), Qwen parser auto-detection (#51169), Kimi K3 `reasoning_effort="none"` (#53043), `n > 1` rejection on `/inference/v1/generate` (#52844), and the `LogprobsTensors` wire schema (#53939).
* **Pooling**: BGE-M3 throughput (+3.13%, #53464) and task validation (#51823), prompts truncated before padding (#54364), parallel arrays truncated with the prompt (#54407, #54509), and batched input throughput in `vllm bench serve` (#53213).
* **CLI & tooling**: `vllm launch` runs the serve argument checks (#52825), `run_batch.py` moved out of the openai folder (#53500) with its upload retry fixed (#50588), `vllm bench` warns on a warm prefix cache for random runs (#53920) and restores multimodal datasets on the `vllm` throughput backend (#52168), and the vLLM recipes tool gained sweep recommendations and alias parsing (#53325, #53946).

## Security
* `cache_salt` length bounded to 1024 to prevent scheduler CPU exhaustion (#54353).
* Oversized media rejected before full download (#51896); `VLLM_MAX_AUDIO_CLIP_FILESIZE_MB` enforced on all audio paths (#53561).
* Decoder prompt-length validation enforced for processors that skip the check (#46588); PyNvVideoCodec decoder slot limit bypass fixed (#52126).
* `api_key` and `hf_token` redacted from startup logs, compile cache factors, and the Rust frontend launch log (#52523, #53625, #53738).

## Dependencies
* FlashInfer 0.6.18 (#54313), huggingface-hub 1.28.0 (#52797), tpu-inference v0.28.0 (#54020), NIXL 1.3.2 (#51777).
* InstantTensor added to CUDA dependencies; the loader is not enabled by default (#52801). CuPy constraint relaxed to exclude only 14.1.0 (#44284).
* ROCm base image: ROCr/CLR update (#53712), rocprofiler-sdk 1.3.2 (#53182), TheRock 7.14 preview (#49925), LMCache connector packages (#51208).
* XPU: vllm_xpu_kernels 0.1.14.1 (#54203), UCX install updated (#53817).
* Build fails closed when the selected precompiled CUDA variant is unavailable (#52545).

## Breaking Changes & Deprecations
* Ten deprecated architectures removed: Arctic, Chameleon, Cheers, Fairseq2Llama, FireRedLID, GritLM, HCXVision, MPT, the `RWForCausalLM` and `StableLMEpochForCausalLM` aliases, and `PrithviGeoSpatialMAE` (superseded by `Terratorch`) (#53608).
* FlexOlmo, Olmo3, Hunyuan V1 and Hunyuan VL are now served through the Transformers modeling backend (#53615).
* PyAV video decoder backend removed; use OpenCV or Torchcodec (#54231).
* `python -m vllm.entrypoints.openai.api_server` is deprecated; use `vllm serve` (#52131).
* Model Runner V2 is the default runner for all models (#53183).
* `prefix_cache_retention_interval` default changed from dense to 0 for SWA/SSM models; the env var is deprecated in favor of the argument (#52216).
* FlashInfer all-reduce enabled by default (#52998).
* `VLLM_TEST_FORCE_FP8_MARLIN` removed in favor of `--linear-backend` / `--moe-backend` (#52182); `VLLM_ROCM_USE_AITER_FP4_ASM_GEMM` removed (#53141); dead `--attention-config.use_prefill_decode_attention` removed (#52557); other long-deprecated parameters cleaned up (#53559).

## New Contributors
* @030611 made their first contribution in https://github.com/vllm-project/vllm/pull/51823
* @92hyungjun made their first contribution in https://github.com/vllm-project/vllm/pull/47272
* @ActiveSky made their first contribution in https://github.com/vllm-project/vllm/pull/52692
* @adisivaprasad made their first contribution in https://github.com/vllm-project/vllm/pull/53854
* @Agoni-02 made their first contribution in https://github.com/vllm-project/vllm/pull/48850
* @AmitMY made their first contribution in https://github.com/vllm-project/vllm/pull/48608
* @Andy365-365 made their first contribution in https://github.com/vllm-project/vllm/pull/52523
* @andyluo7 made their first contribution in https://github.com/vllm-project/vllm/pull/53821
* @AnkitNakhawa made their first contribution in https://github.com/vllm-project/vllm/pull/52491
* @anmolgupt made their first contribution in https://github.com/vllm-project/vllm/pull/52874
* @bobboli made their first contribution in https://github.com/vllm-project/vllm/pull/51924
* @brianosaurus made their first contribution in https://github.com/vllm-project/vllm/pull/52557
* @CalvinXKY made their first contribution in https://github.com/vllm-project/vllm/pull/50858
* @canlahlah made their first contribution in https://github.com/vllm-project/vllm/pull/53409
* @CherryLemon made their first contribution in https://github.com/vllm-project/vllm/pull/53877
* @CHIPMUNK-T0T made their first contribution in https://github.com/vllm-project/vllm/pull/47625
* @cogniera made their first contribution in https://github.com/vllm-project/vllm/pull/53839
* @cr-zhao made their first contribution in https://github.com/vllm-project/vllm/pull/52385
* @daviswer made their first contribution in https://github.com/vllm-project/vllm/pull/52706
* @DCoEngine made their first contribution in https://github.com/vllm-project/vllm/pull/48682
* @dineshchitlangia made their first contribution in https://github.com/vllm-project/vllm/pull/49688
* @dkrisman made their first contribution in https://github.com/vllm-project/vllm/pull/54380
* @dmvevents made their first contribution in https://github.com/vllm-project/vllm/pull/52632
* @Edge-Explorer made their first contribution in https://github.com/vllm-project/vllm/pull/53435
* @eilamc14 made their first contribution in https://github.com/vllm-project/vllm/pull/47640
* @eligotts made their first contribution in https://github.com/vllm-project/vllm/pull/54315
* @Eoin-Houstoun made their first contribution in https://github.com/vllm-project/vllm/pull/51703
* @floatlibai made their first contribution in https://github.com/vllm-project/vllm/pull/52144
* @fuzzifikation made their first contribution in https://github.com/vllm-project/vllm/pull/51034
* @hagaikwa-redhat made their first contribution in https://github.com/vllm-project/vllm/pull/53230
* @haoyangqian made their first contribution in https://github.com/vllm-project/vllm/pull/52690
* @Hert4 made their first contribution in https://github.com/vllm-project/vllm/pull/51157
* @ima-helikoptaaa made their first contribution in https://github.com/vllm-project/vllm/pull/54427
* @jbyczkow made their first contribution in https://github.com/vllm-project/vllm/pull/52174
* @JC-ut0 made their first contribution in https://github.com/vllm-project/vllm/pull/53071
* @jiahaoliang made their first contribution in https://github.com/vllm-project/vllm/pull/51262
* @JiataiWang made their first contribution in https://github.com/vllm-project/vllm/pull/53553
* @jl9876 made their first contribution in https://github.com/vllm-project/vllm/pull/51248
* @JulianZJN made their first contribution in https://github.com/vllm-project/vllm/pull/53253
* @jungjiyu made their first contribution in https://github.com/vllm-project/vllm/pull/53939
* @kyleliang-nv made their first contribution in https://github.com/vllm-project/vllm/pull/51203
* @LH-and-FPGA made their first contribution in https://github.com/vllm-project/vllm/pull/52648
* @li-ukumar made their first contribution in https://github.com/vllm-project/vllm/pull/52571
* @LioEinaudi made their first contribution in https://github.com/vllm-project/vllm/pull/53247
* @LironKesem made their first contribution in https://github.com/vllm-project/vllm/pull/53921
* @Lossfull made their first contribution in https://github.com/vllm-project/vllm/pull/52948
* @lxy-alexander made their first contribution in https://github.com/vllm-project/vllm/pull/52430
* @lxyxinyi made their first contribution in https://github.com/vllm-project/vllm/pull/50809
* @machero made their first contribution in https://github.com/vllm-project/vllm/pull/53531
* @matthewkotila made their first contribution in https://github.com/vllm-project/vllm/pull/48915
* @mhoqueanik made their first contribution in https://github.com/vllm-project/vllm/pull/49636
* @mhuzaifa3 made their first contribution in https://github.com/vllm-project/vllm/pull/53625
* @minjang made their first contribution in https://github.com/vllm-project/vllm/pull/53530
* @MKQuantum made their first contribution in https://github.com/vllm-project/vllm/pull/51866
* @new-TonyWang made their first contribution in https://github.com/vllm-project/vllm/pull/53704
* @nicholaskh-ai made their first contribution in https://github.com/vllm-project/vllm/pull/53819
* @oliverholworthy made their first contribution in https://github.com/vllm-project/vllm/pull/52185
* @prakharPant made their first contribution in https://github.com/vllm-project/vllm/pull/52743
* @Prudhvivuda made their first contribution in https://github.com/vllm-project/vllm/pull/53016
* @rajathpi made their first contribution in https://github.com/vllm-project/vllm/pull/52622
* @ray24777 made their first contribution in https://github.com/vllm-project/vllm/pull/53120
* @roachsinai made their first contribution in https://github.com/vllm-project/vllm/pull/53528
* @RookieCoder-Camera made their first contribution in https://github.com/vllm-project/vllm/pull/49274
* @sandeep-maddipatla made their first contribution in https://github.com/vllm-project/vllm/pull/51777
* @seonjinn made their first contribution in https://github.com/vllm-project/vllm/pull/52204
* @ShengleiFu made their first contribution in https://github.com/vllm-project/vllm/pull/53650
* @shepark made their first contribution in https://github.com/vllm-project/vllm/pull/51302
* @shijuzhao made their first contribution in https://github.com/vllm-project/vllm/pull/45683
* @shipiyouniao made their first contribution in https://github.com/vllm-project/vllm/pull/51979
* @ShuaiShao93 made their first contribution in https://github.com/vllm-project/vllm/pull/53034
* @simon-veitner-redhat made their first contribution in https://github.com/vllm-project/vllm/pull/52980
* @sseanliu made their first contribution in https://github.com/vllm-project/vllm/pull/52041
* @studioego made their first contribution in https://github.com/vllm-project/vllm/pull/52389
* @tanchao made their first contribution in https://github.com/vllm-project/vllm/pull/50191
* @thanhpt1110 made their first contribution in https://github.com/vllm-project/vllm/pull/52720
* @theamalsebastian made their first contribution in https://github.com/vllm-project/vllm/pull/52588
* @thunguo made their first contribution in https://github.com/vllm-project/vllm/pull/53101
* @tolleybot made their first contribution in https://github.com/vllm-project/vllm/pull/51292
* @tommy-asai-sonarsource made their first contribution in https://github.com/vllm-project/vllm/pull/51395
* @tthakkal made their first contribution in https://github.com/vllm-project/vllm/pull/50501
* @ukannika made their first contribution in https://github.com/vllm-project/vllm/pull/52849
* @VBS2004 made their first contribution in https://github.com/vllm-project/vllm/pull/48922
* @wyettzeng made their first contribution in https://github.com/vllm-project/vllm/pull/52209
* @Xuan-1998 made their first contribution in https://github.com/vllm-project/vllm/pull/53008
* @y0hnn made their first contribution in https://github.com/vllm-project/vllm/pull/52002
* @Yiqin-17 made their first contribution in https://github.com/vllm-project/vllm/pull/52925
* @YukioZzz made their first contribution in https://github.com/vllm-project/vllm/pull/51705
* @ZHIHANCHEN03 made their first contribution in https://github.com/vllm-project/vllm/pull/53432
* @Zhou248 made their first contribution in https://github.com/vllm-project/vllm/pull/52560
* @zllion made their first contribution in https://github.com/vllm-project/vllm/pull/46701
* @zwischenraum made their first contribution in https://github.com/vllm-project/vllm/pull/50272

## Contributors
@AndreasKaratzas, @khluu, @mgoin, @taneem-ibrahim, @yewentao256, @njhill, @BugenZhao, @hmellor, @GirasoleY, @DarkLight1337, @mayuyuace, @WoosukKwon, @jeejeelee, @noooop, @jperezdealgaba, @LucasWilkinson, @wzhao18, @stefankoncarevic, @gau-nernst, @okorzh-amd, @ZJY0516, @HollowMan6, @connorcarpenter15, @gcanlin, @aoshen02, @linitra24, @mhuzaifa3, @Isotr0py, @Etelis, @MatthewBonanni, @TheEpicDolphin, @zupengwang, @atalman, @NickLucche, @rasmith, @elvircrn, @JasonKeyiL, @Hotragn, @waizuichougou, @pmanczak, @divakar-amd, @he-yufeng, @xuebwang-amd, @qgallouedec, @esmeetu, @ZeldaHuang, @Prudhvivuda, @chaunceyjiang, @zxd1997066, @louie-tsai, @andyxning, @pisceskkk, @tjtanaa, @qli88, @tlrmchlsmth, @LopezCastroRoberto, @micah-wil, @hongxiayang, @BabyDrangoner, @yimdev, @mganczarenko, @SageMoore, @biswapanda, @sfeng33, @russellb, @itayalroy, @bigPYJ1151, @drakosha, @qwerqwerqwe8688-jpg, @akii96, @shen-shanshan, @meiyeh123, @reidliu41, @yma11, @frgossen, @lukealonso, @tanchao, @Fangzhou-Ai, @vllm-agent, @djramic, @andrewbcohere, @zyongye, @gty111, @ShuoleiWang, @ZHIHANCHEN03, @KurodaKanbei, @alexeldeib, @khushali9, @xiaohuguo2023, @hungnnvidia, @thisjiang, @arpera, @ShengleiFu, @zhenwei-intel, @jungjiyu, @yzong-rh, @charlifu, @hclsys, @Ronald1995, @mkhazraee, @YukioZzz, @theamalsebastian, @030611, @cr-zhao, @jbyczkow, @tommy-asai-sonarsource, @jikunshang, @bobboli, @LH-and-FPGA, @SayHelloToWorld, @kkt-cohere, @floatlibai, @rajathpi, @lxy-alexander, @gangula-karthik, @ActiveSky, @fxmarty-amd, @sstamenk, @mpashkovskii, @LiuYinfeng01, @chaojun-zhang, @Oxygen56, @daviswer, @vineethsaivs, @Agoni-02, @Andy365-365, @wangxiyuan, @haoyangqian, @Naveassaf, @eilamc14, @sseanliu, @y0hnn, @dineshchitlangia, @yiliu30, @Lossfull, @lxyxinyi, @vanshbhatia-amd, @jcotant-inferact, @chengy-sysu, @92hyungjun, @xyang16, @AmitMY, @zllion, @AnkitNakhawa, @dmvevents, @lengrongfu, @JC-ut0, @Eoin-Houstoun, @sagearc, @wjabbour, @sandeep-maddipatla, @seonjinn, @Yiqin-17, @kyleliang-nv, @thanhpt1110, @MKQuantum, @matthewkotila, @vhagor, @tthakkal, @ShuaiShao93, @hagaikwa-redhat, @shijuzhao, @elwhyjay, @Gregory-Pereira, @ErenAta16, @morrison-turnansky, @DCoEngine, @SoluMilken, @xianbaoqian, @kliuae, @nascheme, @waynehacking8, @stecasta, @vMaroon, @zwischenraum, @Rohan138, @Zhou248, @brianosaurus, @anmolgupt, @wyettzeng, @hao-aaron, @frank-suwen, @danisereb, @fuzzifikation, @KernelClint, @thunguo, @studioego, @shipiyouniao, @cjackal, @LioEinaudi, @guan404ming, @libinta, @mhoqueanik, @minjang, @Edge-Explorer, @shepark, @JiataiWang, @jiahaoliang, @therealnaveenkamal, @ColinZ22, @tolleybot, @almogtavor, @simon-veitner-redhat, @hyeongyun0916, @nicholaskh-ai, @mawong-amd, @adisivaprasad, @ray24777, @robertgshaw2-redhat, @new-TonyWang, @oliverholworthy, @fanxingran, @lucianommartins, @hallerite, @avininjamay8, @VBS2004, @omerpaz95, @Sunt-ing, @Hert4, @wangshangsam, @cogniera, @jeffreywang88, @Xarbirus, @fynnsu, @jiahanc, @zixi-qi, @prakharPant, @haic0, @maobaolong, @xinyu-intel, @CHIPMUNK-T0T, @hangy-amd, @afriedri, @canlahlah, @Alnusjaponica, @ukannika, @pranavthakur0-0, @zzaebok, @Xuan-1998, @JulianZJN, @roachsinai, @ivanium, @CalvinXKY, @yu-xin-c, @rchalamala, @QwertyJack, @Kaif10, @yudigege86, @yiz-liu, @dkrisman, @simondanielsson, @machero, @CherryLemon, @ima-helikoptaaa, @RookieCoder-Camera, @LironKesem, @peakcrosser7, @eligotts, @foraxe, @zufangzhu, @tianmu-li, @jl9876, @lucifer1004, @liranschour, @maithilijoshi20, @li-ukumar, @andyluo7, @Zhenzhong1, @faaany, @joerowell, @juhi10071998, @ganeshr10, @Priyjain-amd, @SubSir, @luyixiao95, @djw8605, @codex
