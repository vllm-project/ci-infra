# vLLM v0.24.0 Release Notes

## Highlights

This release features 571 commits from 256 contributors (77 new)!

* **MiniMax-M3**: Added support for the new **MiniMax-M3** model (#45381), with a fast follow-on of BF16/FP8 indexer via MSA (#45892), MXFP4 support (#45896), FP8 sparse GQA (#45744), and extensive AMD/ROCm tuning — mxfp8 MoE/linear on gfx950 (#45725), fp8_per_channel for bf16 weights on MI300X (#45854), FP8 KV-cache fix (#45720), and packed-modules mapping (#45794). A MiniMax-M2 perf regression was also fixed (#45935).
* **DeepSeek-V4 keeps maturing**: Following its debut, DeepSeek-V4 received another large optimization pass — a FlashInfer sparse index cache (2–4% TTFT) (#45863), prefill chunk-planning optimization (4% E2E throughput) (#45061), a cluster-cooperative topK kernel for low-latency (#43008), contiguous per-block KV allocations (#44577), TEP=16 for the block-FP8 shared expert (#46001), and native DSA indexer decode for `next_n > 2` on SM100 (#45322). It is now enabled on **SM120** alongside GLM-5.1 (#43477), with XPU (#44144, #44517, #45240) and ROCm (#44899, #45103, #45681) attention/MoE paths added.
* **Model Runner V2 (MRv2) continues to expand**: MRv2 now **supports quantized models by default** (#44446), enables **GraniteMoE by default** (#45461), and gained migration of Qwen + DeepSeek-V2 MoE models (#42667), DFlash speculative decoding (#44586), and more accurate FP32 Gumbel sampling (#45996).
* **Streaming Parser Engine**: A new streaming parser engine unifies tool-call/reasoning parsing across models, with parsers for Qwen3 (#45413), MiniMax-M2 (#45701), GLM-4.7/5.1/5.2 (#45915), and Nemotron V3 (#45755).
* **Diffusion LLMs**: Added **DiffusionGemma** (#45163), including a CPU path (#45690) and structured-output guardrails for diffusion decoders (#45468).
* **WideEP / DeepEP v2**: Integrated **DeepEP v2** for expert parallelism (#41183), with follow-on robustness fixes (#46404, #46432).
* **Rust frontend matures further**: Added API-key authentication (#44321), CORS (#45753), `/tokenize` + `/detokenize` (#44222), `/pause` `/resume` `/is_paused` (#44499), `/abort_requests` (#44382), `/get_world_size` (#44801), `thinking_token_budget` (#46137), a Python bridge for Rust tool parsers (#44624), and many new parsers and validation paths.
* **Device selection change**: vLLM no longer sets `CUDA_VISIBLE_DEVICES` internally; a new `device_ids` argument is provided instead (#45026). On ROCm, a deprecation window for `CUDA_VISIBLE_DEVICES` has begun (#46636).

### Model Support
* **New models**: MiniMax-M3 (#45381), DiffusionGemma (#45163) + Gemma Diffusion on CPU (#45690), Hierarchical Reasoning Model — Text / HrmTextForCausalLM (#43098), OpenMOSS (#44124).
* **Gemma 4**: Unified FlashAttention (FA4) across all layers + `mm_prefix` support (#42175); many parser/serving fixes — forced-JSON skip for required/named tool choice (#45795), parsing with thinking disabled (#45832), streaming reasoning-state init (#45852), reasoning rendering on assistant turns (#45867), offline-parser truncation/token-leak fix (#45553); legacy Gemma4 parsers replaced with an engine-based implementation (#45588).
* **DeepSeek-V4**: OOM fix (#44914), MTP projection prefixing (#44821), supported KV-cache dtypes (#44892).
* **Qwen / multimodal**: Qwen3-VL video loader (#44412), Qwen2-VL/Qwen2.5-VL processor-mapped video loader (#45555), Qwen3-VL multi-video processing optimization (#46026) and multi-video crash fix (#46305), Qwen3-Omni VIT cu_seqlens device fix (#44264), fused qk-rmsnorm-rope-gate for Qwen3.5 (#44176), Qwen3.5 EP weight-loading fix (#45002).
* **ViT full CUDA graph**: GLM-4.1V (#40576), DeepSeek-OCR dual-path (#43586), Kimi-VL (#41992), mllama4 (#40660), Lfm2VL encoder (#44930).
* **Other model fixes**: Llama4 weight loading (#45047) and streamed loading to avoid host-OOM (#44645), MiMo v2.x QKV TP sharding + FP4 (#45200), ColQwen3.5 retrieval correctness (#46108), EXAONE-4.5 vision encoder (#45073), MiDashengLM TP>1 audio-encoder crash (#44408), MiniCPM-o/V device-placement and image-size fixes (#43844, #42332, #44980, #45244), Cohere2 MoE weight loading + parser (#44747, #44907), Nemotron V3 reasoning-as-content (#39091), ColBERT AutoWeightsLoader + query/document embedding io processor (#44999, #45210).
* **Kernels**: GLM-5 TRT-LLM ragged MLA prefill dimensions (#43525), GLM-5 router GEMM (#46385).

### Engine Core
* **Model Runner V2**: Quantized models by default (#44446), GraniteMoE default (#45461), Qwen/DSv2 MoE migration (#42667), DFlash (#44586), simplified async output handling (#45442), attention-group split on `num_heads_q` (#45564), LoRA warmup fix (#35536), more accurate FP32 Gumbel sampling (#45996), `min_tokens` off-by-one fix in the V2 GPU sampler (#46243), plus assorted model/config compatibility fixes (#45868).
* **Speculative decoding**: Dynamic SD (#32374); DFlash with FlashInfer (#43081), mixed KV page sizes (#45181), and Qwen3Next targets (#45319); EAGLE3 support for Qwen3 (#43132); reduced TP communication for large-vocab drafts (#39419); race fix in async accepted counts (#45100); EAGLE multimodal encoder cache fixes (#46315).
* **KV cache & scheduler**: KV-cache watermark to reduce preemptions (#44594), two-phase allocation for cross-group prefix-cache hits (#44409), Marconi-style admission policy for hybrid cache (#37898), prefix-cache retention for Mamba/linear attention (#45845), DS Mamba tail-copy for MTP align mode (#45473), reduced scheduler copy overhead (#45840).
* **Attention**: Re-enabled cross-layer KV cache layout for MLA via stride-aware kernels (#45111), MLA prefill FA4 fp8 output (#43050), FlexAttention custom mask mods made fully cudagraphable (#45232), triton diff-kv backend for MiMo (#41797), FlashMLA sparse accuracy fix (#36616).
* **Weight loading & core**: fastsafetensors `ParallelLoader` for weight loading (#40183), release of cached device memory under pressure on UMA GPUs (#45179), structured outputs for beam search (#35022), `device_ids` arg / no internal `CUDA_VISIBLE_DEVICES` (#45026), graceful fallback when `numactl --membind` is blocked (#45438), config-class registration before tokenizer init (#40299), async scheduling with prompt embeds for multimodal models (#45673).

### Large Scale Serving & Distributed
* **Expert parallel**: DeepEP v2 integration (#41183) with token-bound and topk-index fixes (#46404, #46432); NIXL EP — DBO with NIXL EP (#45275), top-k index dtype query (#45298), NVFP4 post-receive quantization skip (#45606), elastic-EP communicator (#45013); reject NCCL-based EPLB with async EPLB (#44978).
* **KV connectors / disaggregated serving**: KV push from prefill to decode via NIXL (#35264); per-region KV transfer classification for mixed full-attn + MLA groups (#44583); Mooncake pipeline-parallel PD support (#44528), async lookup (#45659), compact chunk-hash zero-copy lookup (#45969), SWA-block skipping (#45444); P/D fixes with DP supervisor (#46628) and DSV4 disaggregation (#45831); removed `P2pNcclConnector` (#44854).
* **KV offloading**: Multi-tier async batched lookup (#44193), packed HMA KV-cache layout (#46205, gated #46252), parallel-agnostic fs-tier cache (#44733), offloading-manager stats (#35669) and labeled/CPU-usage metrics (#45957, #45737), self-describing KV events (#43468), non-blocking idle flush (#45595), and numerous correctness/race fixes (#44784, #45823, #46231, #46278).
* **Distributed core**: Prefill step cadence for better non-PD DP balancing (#44558), KV-event map encoding (#42892), one-shot fused all-reduce PDL NaN fix (#45448).

### Hardware & Performance
* **NVIDIA / kernels**: SM90 CUTLASS FP8 mm odd-M support via swap_ab (180–290% kernel speedup) (#44572), tuned `fused_moe` FP8 for Qwen3-Next-80B on H100 (+25%) (#44830), native DSA indexer decode on SM100 (#45322), cluster-cooperative topK for DeepSeek low-latency (#43008), PDL support for DeepGEMM (#46006), FlashInfer cutedsl NVFP4 GEMM (#42235) and cute-dsl MXFP8 linear kernel (#46393), new Helion kernels for FP8/RMSNorm quant (#36902, #33790, #36895, #34432).
* **torch stable ABI**: Continued (and completed) migration of kernels to the libtorch stable ABI — MoE [10c/n] (#44565), Marlin [11a/n] (#45176), Machete [11b/n] (#45304), final `_C` library migration [12/n] (#45415).
* **AMD ROCm**: Torch 2.11 (#45362); fused AR + RMSNorm + per-group FP8 quant (#42864), fused softplus-sqrt-topk MoE router under AITER (#44945), DSv4 flash-decode split-K kernel (#44899) and inverse-RoPE fusion (#45103), W4A16 FlyDSL MoE (#44400), A8W4 MoE CDNA4 swizzle gate for gpt-oss (#44804); deprecation window begun for `CUDA_VISIBLE_DEVICES` on ROCm (#46636).
* **Intel XPU**: Sequence-parallel support (#38608), torch-xpu 2.12 (#42262), vllm-xpu-kernels v0.1.10 (#40367), W4A16 int4 group_size=32 MoE (#45136), DeepSeek-V4 attention/MoE paths (#44144, #44517, #45240), top-p sampling correctness fix (#44470).
* **CPU & other architectures**: 2.5× faster ASR CPU preprocessing via multi-threading (#44612), CPU W4A16 INT4 MoE (#43409), cgroup memory-limit-aware KV cache sizing (#45086), RISC-V oneDNN W8A8 INT8 (#44478) and RVV micro-GEMM for WNA16 (#44324), pinned memory for WSL2 (#41496), ZenCPU runtime logging (#42726).
* **TPU**: tpu-inference upgraded to v0.22.1 (#45793).
* **Misc perf**: `VLLM_TRITON_FORCE_FIRST_CONFIG` to skip Triton autotuning (#42425), Triton recompile detection (#45631), fused multi-group block-table staged writes (#44944).

### Quantization
* **Online & mixed-precision**: Online FP8 per-token-per-channel (PTPC) quantization (#44132); `modelopt_mixed` support extended to Ampere/SM80-86 (#45306) and Turing/SM75 (#45375).
* **FP4 / MXFP**: FlashInfer cutedsl NVFP4 GEMM backend (#42235) and cute-dsl MXFP8 linear kernel (#46393), MXFP4 W4A4 MoE CUTLASS E8M0 scale fix (#43557), SwiGLU clamp wired for NVFP4 MoE on non-Blackwell (#45836), `flashinfer_cutlass` allowed as a clamped NVFP4 MoE backend (#46492), NVFP4/OCP MX MoE emulation fix (#46254), FP8 MoE re-enabled on NVIDIA Thor (#46339).
* **GGUF / compressed-tensors / AWQ**: GGUF quantization migrated to a plugin (#39612), compressed-tensors WNA16 MoE actorder fix (#41161) and KV-cache-scheme rejection (#45312), AWQ format on XPU (#43404) and AWQ dequantize fix on Intel XPU (#42727).
* **Kernels & correctness**: QuantizedActivation linear-kernel contract (#44260), consolidated Marlin thread-tile padding (#45295), FP8 weight layout canonicalized to (K, N) (#44735), corrupt-output fix for MoE FP8 with LoRAs loaded (#42120), symmetric-quant regression fix in GPTQ/CT MoE (#45656), `fp8_e5m2` KV cache allowed for non-fp8 checkpoints (#45040).

### API & Frontend
* **Tool calling & parsing**: Strict mode for tool calling in Chat Completions (#45003) and Responses API (#45396); new Streaming Parser Engine (#45413) with Qwen3, MiniMax-M2 (#45701), GLM-4.7/5.1/5.2 (#45915), Nemotron V3 (#45755) parsers; unified Parser consolidation in chat serving (#45548); numerous parser correctness fixes (#46047, #46091, #46159, #45763, #46351, #43984).
* **OpenAI / Responses**: Real `/v1/embeddings` support for messages + `chat_template_kwargs` (#45173), multimodal token counts in `usage.prompt_tokens_details` (#45458), omit empty `tool_calls` from chat responses (#44105), Responses API streaming `function_call` id fix (#44608), Harmony refactor of streaming/non-streaming paths (#45171, #45104).
* **Anthropic Messages API**: Cache-usage reporting in `/v1/messages` (#40912), mid-conversation system-message handling (#46025), inline system-message position preserved for prefix caching (#44602), `tool_use` argument-dropping fix (#45287).
* **Rust frontend**: API-key auth (#44321), CORS (#45753), `/tokenize` + `/detokenize` (#44222), `/pause` `/resume` `/is_paused` (#44499), `/abort_requests` (#44382), `/get_world_size` (#44801), `thinking_token_budget` (#46137), `parallel_tool_calls=false` (#44760), continuous usage stats (#43965), model metadata in `/v1/models` (#45950), Python bridge for Rust tool parsers (#44624), dedicated runtime for HTTP/ZMQ (#46051), and many validation/correctness fixes.
* **Metrics**: `vllm:tool_call_parser_invocations_total` (#44448), group-aware KV cache capacity in `vllm:cache_config_info` (#42206), MLA attention metrics for DeepSeek MFU estimation (#39457).
* **Pooling / embeddings**: Validation for Cohere `/v2/embed` input exclusivity (#45640), non-negative rerank `top_n` (#46119), matryoshka embedding dimension bounds (#46313).
* **Benchmarks**: BFCL tool-calling dataset for `vllm bench serve` (#42457), multi-turn benchmark api_key/custom headers (#44516), tokenizer-mismatch auto-correction (#44708).

### Security

This release ships another coordinated security-hardening batch (much of it from security researcher @jperezdealgaba).

* **Denial of service**: Audio decompression bomb in the speech-to-text endpoint (#44970), remote DoS via invalid recovered-token reinjection in speculative decoding (#44744), DoS via `prompt_embeds` on M-RoPE models (#45252), regex-compilation timeout guard in structured outputs (#45118), audio upload size limit before full materialization (#45510), audio decode duration limit in the chat-completions path (#45908).
* **Information disclosure**: int32 truncation in the GGUF dequantize kernels (#44971).
* **Input validation & hardening**: Image EXIF orientation and tRNS transparency handling (#44974), rejection of non-finite `temperature`/`repetition_penalty` (#45116), `sanitize_message` applied to Anthropic and STT error paths (#45119).
* **Dependencies**: Upgrade Starlette to ≥ 1.0.1 to fix CVE-2026-48710 (#45675).

### Dependencies
* Torch 2.11 on ROCm (#45362), torch-xpu 2.12 (#42262), tpu-inference v0.22.1 (#45793), NIXL v0.10.1 for XPU (#40287), Starlette ≥ 1.0.1 (#45675).
* `mistral_common` is now optional via deferred import (#45305); CUDA Dockerfiles upgraded from GCC 10 to GCC 12 for C++20 (#44923); spinloop extension skipped on Python < 3.11 (#44783).

### Deprecations & Removals
* **Removed models**: ERNIE (obsolete) (#45127), Xverse (#45638), Dots1 (#45637), Bamba (#45990), Mono-InternVL (#45129), InternLM registry alias (#45128).
* **Deprecated**: First-generation Qwen and QwenVL models (#45131), Transformers v4 support (#45161), `CUDA_VISIBLE_DEVICES` on ROCm (#46636); general deprecations for v0.23/v0.24 (#44992).

## New Contributors

* @abcd1927 made their first contribution in https://github.com/vllm-project/vllm/pull/43098
* @Achyuthan-S made their first contribution in https://github.com/vllm-project/vllm/pull/44795
* @Alex-ai-future made their first contribution in https://github.com/vllm-project/vllm/pull/45905
* @alexbi29 made their first contribution in https://github.com/vllm-project/vllm/pull/45763
* @amanchugh89 made their first contribution in https://github.com/vllm-project/vllm/pull/45840
* @ankrovv made their first contribution in https://github.com/vllm-project/vllm/pull/44608
* @anony-mous-e made their first contribution in https://github.com/vllm-project/vllm/pull/45412
* @appleparan made their first contribution in https://github.com/vllm-project/vllm/pull/45073
* @ashishpatel26 made their first contribution in https://github.com/vllm-project/vllm/pull/43984
* @Bot1822 made their first contribution in https://github.com/vllm-project/vllm/pull/44053
* @ByteFlowing1337 made their first contribution in https://github.com/vllm-project/vllm/pull/45988
* @Change72 made their first contribution in https://github.com/vllm-project/vllm/pull/43756
* @coder3101 made their first contribution in https://github.com/vllm-project/vllm/pull/44801
* @cquil11 made their first contribution in https://github.com/vllm-project/vllm/pull/45720
* @dmaniloff made their first contribution in https://github.com/vllm-project/vllm/pull/40470
* @factnn made their first contribution in https://github.com/vllm-project/vllm/pull/44955
* @FAUST-BENCHOU made their first contribution in https://github.com/vllm-project/vllm/pull/44760
* @felix0080 made their first contribution in https://github.com/vllm-project/vllm/pull/44602
* @gitbisector made their first contribution in https://github.com/vllm-project/vllm/pull/40183
* @gq112 made their first contribution in https://github.com/vllm-project/vllm/pull/43081
* @guan404ming made their first contribution in https://github.com/vllm-project/vllm/pull/35022
* @HanHan009527 made their first contribution in https://github.com/vllm-project/vllm/pull/44528
* @hello-args made their first contribution in https://github.com/vllm-project/vllm/pull/44109
* @HumphreySun98 made their first contribution in https://github.com/vllm-project/vllm/pull/45466
* @j-i-l made their first contribution in https://github.com/vllm-project/vllm/pull/45319
* @JasonLi314 made their first contribution in https://github.com/vllm-project/vllm/pull/45255
* @jeffye-dev made their first contribution in https://github.com/vllm-project/vllm/pull/43595
* @jimmy-evo made their first contribution in https://github.com/vllm-project/vllm/pull/44516
* @jjppp made their first contribution in https://github.com/vllm-project/vllm/pull/45217
* @JOSH1024 made their first contribution in https://github.com/vllm-project/vllm/pull/44784
* @junkang1991 made their first contribution in https://github.com/vllm-project/vllm/pull/46039
* @KaletoAI made their first contribution in https://github.com/vllm-project/vllm/pull/43495
* @kliukovkin made their first contribution in https://github.com/vllm-project/vllm/pull/43724
* @littlecircle0730 made their first contribution in https://github.com/vllm-project/vllm/pull/44750
* @llx-08 made their first contribution in https://github.com/vllm-project/vllm/pull/45357
* @m4r1k made their first contribution in https://github.com/vllm-project/vllm/pull/45795
* @martin-kukla made their first contribution in https://github.com/vllm-project/vllm/pull/45417
* @MichaelCao0 made their first contribution in https://github.com/vllm-project/vllm/pull/46398
* @mrn3088 made their first contribution in https://github.com/vllm-project/vllm/pull/45383
* @nataliepjlin made their first contribution in https://github.com/vllm-project/vllm/pull/45218
* @nehmathe2 made their first contribution in https://github.com/vllm-project/vllm/pull/44912
* @nikhilesh-csa made their first contribution in https://github.com/vllm-project/vllm/pull/45852
* @nv-nedelman-1 made their first contribution in https://github.com/vllm-project/vllm/pull/42120
* @Oseltamivir made their first contribution in https://github.com/vllm-project/vllm/pull/45879
* @parthash0804 made their first contribution in https://github.com/vllm-project/vllm/pull/43844
* @pjdurden made their first contribution in https://github.com/vllm-project/vllm/pull/44942
* @pst2154 made their first contribution in https://github.com/vllm-project/vllm/pull/45181
* @Saddss made their first contribution in https://github.com/vllm-project/vllm/pull/44409
* @sahilsGit made their first contribution in https://github.com/vllm-project/vllm/pull/44499
* @sasindharan made their first contribution in https://github.com/vllm-project/vllm/pull/44383
* @shantipriya-amd made their first contribution in https://github.com/vllm-project/vllm/pull/39498
* @Sirius29 made their first contribution in https://github.com/vllm-project/vllm/pull/46026
* @srajabos made their first contribution in https://github.com/vllm-project/vllm/pull/44665
* @sridhar-3009 made their first contribution in https://github.com/vllm-project/vllm/pull/44055
* @stefankoncarevic made their first contribution in https://github.com/vllm-project/vllm/pull/45706
* @sunnweiwei made their first contribution in https://github.com/vllm-project/vllm/pull/45100
* @TanNgocDo made their first contribution in https://github.com/vllm-project/vllm/pull/44222
* @thisisjimmyfb made their first contribution in https://github.com/vllm-project/vllm/pull/41496
* @tykow made their first contribution in https://github.com/vllm-project/vllm/pull/44663
* @V-3604 made their first contribution in https://github.com/vllm-project/vllm/pull/43362
* @vincentzed made their first contribution in https://github.com/vllm-project/vllm/pull/44930
* @vraiti made their first contribution in https://github.com/vllm-project/vllm/pull/42331
* @wangjiaxin99 made their first contribution in https://github.com/vllm-project/vllm/pull/45794
* @waynehacking8 made their first contribution in https://github.com/vllm-project/vllm/pull/45376
* @x41lakazam made their first contribution in https://github.com/vllm-project/vllm/pull/43300
* @xiaguan made their first contribution in https://github.com/vllm-project/vllm/pull/45286
* @xiaohuguo2023 made their first contribution in https://github.com/vllm-project/vllm/pull/44804
* @xin3he made their first contribution in https://github.com/vllm-project/vllm/pull/43557
* @xx-thomas made their first contribution in https://github.com/vllm-project/vllm/pull/45210
* @yangdian96 made their first contribution in https://github.com/vllm-project/vllm/pull/44173
* @YellowFoxH4XOR made their first contribution in https://github.com/vllm-project/vllm/pull/45057
* @yzhan1 made their first contribution in https://github.com/vllm-project/vllm/pull/44552
* @Zedong-Liu made their first contribution in https://github.com/vllm-project/vllm/pull/45361
* @ZewenShen-Cohere made their first contribution in https://github.com/vllm-project/vllm/pull/41161
* @zhangshuoming990105 made their first contribution in https://github.com/vllm-project/vllm/pull/40912
* @ZiguanWang made their first contribution in https://github.com/vllm-project/vllm/pull/43981
* @zlxi02 made their first contribution in https://github.com/vllm-project/vllm/pull/44595

## Contributors

Thank you to everyone who made this release possible!

@yewentao256, @Sunt-ing, @jperezdealgaba, @AndreasKaratzas, @BugenZhao, @sfeng33, @njhill, @micah-wil, @bbrowning, @mgoin, @jeejeelee, @hmellor, @tlrmchlsmth, @xianbaoqian, @mmangkad, @jikunshang, @Dao007forever, @zhenwei-intel, @noooop, @Isotr0py, @ivanium, @reidliu41, @varun-sundar-rabindranath, @chaunceyjiang, @WoosukKwon, @mawong-amd, @zxd1997066, @chaojun-zhang, @NickLucche, @bigPYJ1151, @ZJY0516, @charlifu, @yzong-rh, @divakar-amd, @khluu, @cleonard530, @wseaton, @xiaohongchen1991, @ywang96, @taneem-ibrahim, @mikekg, @itayalroy, @Alex-ai-future, @sahilsGit, @bnellnm, @littlecircle0730, @majian4work, @ricky-chaoju, @ronensc, @Fangzhou-Ai, @lucianommartins, @Srinivasoo7, @zyongye, @Rohan138, @Etelis, @wentian-byte, @ekagra-ranjan, @LucasWilkinson, @tahsintunan, @waynehacking8, @gau-nernst, @tuukkjs, @stefankoncarevic, @Palaiologos1453, @lucifer1004, @jmamou, @liulanze, @Terrencezzj, @Change72, @LopezCastroRoberto, @he-yufeng, @benchislett, @juliendenize, @s3woz, @panpan0000, @ilmarkov, @zixi-qi, @wcynb1023, @fynnsu, @ZhanqiuHu, @yuwenzho, @tdoublep, @MatthewBonanni, @hickeyma, @majunze2001, @mrn3088, @Yejing-Lai, @vllmellm, @Saddss, @DarkLight1337, @hongxiayang, @m4r1k, @qli88, @jonathanc-n, @felix0080, @djramic, @aoshen02, @fxmarty-amd, @simon-mo, @llsj14, @akii96, @walterbm, @dmaniloff, @zlxi02, @grYe99, @jeffye-dev, @parthash0804, @qyYue1389, @sagearc, @maeehart, @TanNgocDo, @cinnamonica02, @zucchini-nlp, @tykow, @mganczarenko, @yangdian96, @jimmy-evo, @YellowFoxH4XOR, @yzhan1, @shenoyvvarun, @yufufi, @laviier, @xiaohuguo2023, @EanWang211123, @JartX, @shantipriya-amd, @askliar, @hallerite, @appleparan, @effi-ofer, @angelayi, @TheCodeWrangler, @DanBlanaru, @ankrovv, @velonica0, @pjdurden, @cyyever, @wjinxu, @kliukovkin, @x41lakazam, @Jasen2201, @r-barnes, @tc-mb, @nataliepjlin, @KaletoAI, @WineChord, @fangyuchu, @vraiti, @nascheme, @jjppp, @sasindharan, @xiaguan, @snadampal, @chfeng-cs, @thillai-c, @guan404ming, @sridhar-3009, @vincentzed, @j-i-l, @rjrock, @abinggo, @anony-mous-e, @Achyuthan-S, @Harry-Chen, @mfylcek, @amd-asalykov, @noa-neria, @maobaolong, @TheEpicDolphin, @FAUST-BENCHOU, @martin-kukla, @xin3he, @ZiguanWang, @youkaichao, @factnn, @llx-08, @xx-thomas, @gitbisector, @Bortlesboat, @thisisjimmyfb, @JOSH1024, @wendyliu235, @wangxiyuan, @shen-shanshan, @HanHan009527, @amd-lalithnc, @netanel-haber, @fuscof-ibm, @AjAnubolu, @carlyou, @abcd1927, @CienetStingLin, @kouroshHakha, @alexbi29, @jesse996, @sungsooha, @andakai, @cquil11, @nehmathe2, @liangel-02, @hello-args, @j9smith, @nikhilesh-csa, @ruocco, @oguzhankir, @yiliu30, @xaguilar-amd, @amirkl94, @danisereb, @wangjiaxin99, @shanjiaz, @Oseltamivir, @alexeldeib, @wzhao18, @coder3101, @lyd1992, @markmc, @ashishpatel26, @HumphreySun98, @ByteFlowing1337, @nv-nedelman-1, @JaredforReal, @sammshen, @okorzh-amd, @muhammadfawaz1, @vadiklyutiy, @JasonLi314, @SumanthRH, @Sirius29, @tjtanaa, @zhangshuoming990105, @amanchugh89, @umut-polat, @srajabos, @junkang1991, @pst2154, @WindChimeRan, @Zedong-Liu, @gq112, @sunnweiwei, @athrael-soju, @EazyReal, @Liangliang-Ma, @jinzhen-lin, @V-3604, @aarushjain29, @ZewenShen-Cohere, @Bot1822, @BowenBao, @MichaelCao0, @tanpinsiang, @QwertyJack, @nagisa-kunhah, @Meihan-chen, @robertgshaw2-redhat
