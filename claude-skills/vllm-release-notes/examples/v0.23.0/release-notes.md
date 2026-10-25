# vLLM v0.23.0 Release Notes

## Highlights

This release features 426 commits from 203 contributors (63 new)!

* **DeepSeek-V4 matures across backends**: Following its introduction in v0.22.0, DeepSeek-V4 received another large hardening and optimization pass. Its sparse MLA metadata is now decoupled from DeepSeek-V3.2 (#44699), it gained a TRTLLM-gen attention kernel (#43827), EPLB support for the Mega-MoE (#43339), a CuTe DSL sparse-compressor path (#43584) with compressor refactors (#43690, #43710), a fused q-pad kernel (#43162), selective prefix-cache retention for sliding-window KV cache (#43447), and an index-share feature for DSA MTP (#44420). The model was also detached from `torch.compile` (#43746, #43891), its attention and RoPE paths were refactored (#44569, #44262, #43926), an XPU attention decode path was added (#42953), and ROCm gained Tilelang MHC (#43679) while dropping the MegaMoE integration there (#43629).
* **Model Runner V2 expands to more dense models**: MRv2 is now selected by default for **Llama and Mistral dense models** (#43458) in addition to Qwen3. It gained a FlashInfer sampler (#42472), breakable CUDA graphs (#44050), pipeline-parallel bubble elimination (#42187), kernel block-size support for hybrid models (#38831), and Gemma 4 MTP (#43241).
* **Rust frontend grows up**: The experimental Rust frontend added a streaming `generate` endpoint (#43779), dynamic LoRA endpoints (#43778), `/version` (#43854) and `/server_info` (#43942) endpoints, a server-router extension hook (#43774), request-ID headers (#43883), and many new tool parsers (InternLM2 #43481, hy_v3 #43872, Phi-4-mini #44213, Gemma4 #43850).
* **Gemma 4**: Added encoder-free **Gemma 4 Unified** support (#44429) and Gemma 4 MTP (#43241), plus numerous accuracy and startup fixes.
* **Transformers v5 compatibility**: Continued work toward Transformers v5, with vendored MiniCPM-V/O processors (#44282) and compatibility fixes for Sarvam (#38804) and Voxtral (transformers≥5.10) (#44559).
* **Multi-tier KV cache offloading**: The offloading framework gained an **object-store secondary tier** (#41968), HMA enabled by default for capable connectors (#41847), tiering support for HMA models (#44287), and a per-request offloading policy via the `on_new_request` lifecycle hook (#43205).
* **Unified parser**: Reasoning and tool-call parsing are now unified behind a single `Parser.parse()` interface (#44267), with the Responses parser migrated to it (#42977).

### Model Support
* **New models**: Step-3.7-Flash (#43859), Cosmos3 Reasoner (#43356), Gemma 4 Unified encoder-free (#44429), JetBrains Mellum v2 (#43992), Granite Speech Plus (#43519), Cohere Mini Code (#44707).
* **Gemma 4**: Encoder-free Unified support (#44429), MTP (#43241), native ViT linear layers (#43798), vision-embedder excluded from quantization (#44571), and fixes for MTP under TP>1 (#43909), block-table mismatch under concurrency (#43982), transformers-processor startup crash (#44232), and CPU init (#44615).
* **Transformers v5**: Vendor MiniCPM-V/O processors (#44282), Sarvam compat (#38804), Voxtral `fetch_audio` for transformers≥5.10 (#44559).
* **Model fixes & enhancements**: Qwen3-VL/Qwen3-omni-thinker deepstack accuracy under `torch.compile` (#43617), EVS for Qwen3-VL (#44205), GLM-5.1 PP loading (#42944), GLM-4.1V processor logits (#43575), GLM-4.6V video loader (#44417), OlmoHybrid init (#43846), HyperCLOVAX remote-code removal (#43860), Bailing-MoE rotary factor (#43770), Step3 PP residual KeyError (#37622), MiniCPM-V-4.6 video (#44509), MiniCPM-O audio unpadding (#38053), MiniCPM-V batched preprocessing (#44609), FunASR-Nano init (#44215), Cohere routing method (#44021), Kimi-K2.5 FlashInfer ViT metadata (#44493).
* **Multimodal**: Auto-select registered video loader for VLMs (#44126), O(log n) multimodal item handling per step (#44212), local image encoding in benchmarks (#43843), interleaved custom image benchmark datasets (#43636).
* **Pooling/Classification**: Proper exceptions for pooling UX (#44593), `extra_repr()` for pooler classes (#44805), LoRA-adapter-name pooling fix (#44410), resettled generative scoring entrypoint (#44153), expanded pooler unit tests (#43818, #44471).
* **Refactor**: AutoWeightsLoader for InternLM2 (#38278).

### Engine Core
* **Model Runner V2**: Default for Llama and Mistral dense models (#43458), FlashInfer sampler (#42472), breakable CUDA graphs (#44050), removed Eagle's dedicated CUDA graph pool (#44078), pipeline-parallel bubble elimination (#42187), kernel block size for hybrid models (#38831), zeroing of freshly allocated KV blocks for hybrid + FP8 KV cache (#43990), actual batch `max_seq_len` for attention metadata (#43991), KVConnector + PP cleanup (#43732), KV-connector handling in the spec-decode case (#43719), speculator-prefill warmup/capture (#44253).
* **Speculative decoding (DFlash)**: Causal DFlash (#43445), proper lookahead-slot allocation (#43733), prefix-cache corruption fix (#42971); attention-group split by `num_heads_q` for drafts (#43543), EAGLE/MTP lookahead caching in the SWA prefix-cache mask (#44082).
* **Attention & hybrid/Mamba**: FlexAttention/FlashAttention num-blocks-first layouts (#42095), OOT MLA prefill backend registration (#43325), FlashAttention upstream sync (#44065), Mamba LINEAR attention-module refactor (#43556), corrupted MLA + linear attention fix (#43961), KDA conv-state unification (#44539) and gate/cumsum fusion (#43667), Mamba SSD `do_not_specialize` (#43803), GDN prefill kernel for SM100 (#43273), Qwen3.5 mixed prefill+decode split routing (#44700), MiniMax-M2 gate kernel (#38445).
* **KV cache & scheduler**: Pluggable `KVCacheSpec` (#37505), `scheduler_block_size` threaded into KVCacheManager/Coordinator (#44165), `max_concurrent_batches` moved to `VllmConfig` (#44274), KV-cache scale boilerplate removed from weight loading (#43167).
* **Core**: Freeze the garbage collector in workers after model init (#44363), sparse NCCL weight transfer for in-place updates (#40096), RunAI streamer tensor-buffer reuse fix during weight loading (#43464), early CUDA init fix (#43791), graceful spinloop ext-load failure handling (#43659), scheduled-function deprecations (#43358).

### Large Scale Serving & Distributed
* **KV cache offloading**: Object-store secondary tier (#41968), HMA on by default for capable connectors (#41847) and tiering (#44287), per-request offloading policy (`on_new_request`) (#43205) and `on_schedule_end()` hook (#44206), token-offset selective offload (#39983), skip decode-phase blocks in CPU offload (#43797), page-size block alignment (#43689), Triton fast-path for small CPU→GPU `swap_blocks_batch` (#42212), stale sliding-window block fix (#42959).
* **KV connectors / disaggregated serving**: PP-aware handshake aggregation and intermediate-PP output plumbing (#43720), multiple-async-KV-load deadlock fix (#44560), Nixl Mamba prefix-caching mode (#42554), NixlConnector `kv_both` role deprecation cycle (#43874), Mooncake fixes (#43742, #44103, #42694), LMCache `LMCacheMPConnector` (#42865), EC connector shutdown API (#42423) and non-blocking lookup (#41627), KV-transfer tokens excluded from `iteration_tokens_total` (#43346).
* **EPLB**: Async EPLB by default (#43219), EPLB for DeepSeek-V4 Mega-MoE (#43339), Nixl zero-copy EPLB transfers (#41633).
* **Data parallel**: DP Ray placement groups on specific nodes (#44669) and grouped-node allocation fix (#43998), DP-coordinator startup timeout raised to 120s (#42343), per-GPU-worker RDMA NIC selection (#42083), multi-API-server startup fixes — TOCTOU `EADDRINUSE` race (#42585) and hard-coded-timeout fix (#43768).

### Hardware & Performance
* **NVIDIA / kernels**: Triton MoE backend on Hopper by default (#44220), CUTLASS FP8 scaled-mm padding bypass (+20%) (#43706), MoE-permute buffer pre-allocation (+9–14%) (#43014), `Fp8BlockScaledMM` `new_empty()` optimization (#43677), tuned `selective_state_update` for H200/RTX PRO (#44251), Inductor fast-path fallback for vLLM/AITER custom ops (#42129), Gemma RMS all-reduce fusion (#42646), NUMA auto-binding on DGX B300 (#43270).
* **AMD ROCm**: ROCm 7.2.3 (#43136), AITER v0.1.13.post1 (#44265), native W4A16 (#41394) and fused-MoE W4A16 HIP (#44075) kernels for RDNA3 (gfx1100), AITER top-k/top-p sampler by default (#43331), attention-sink support in AITER FA (#43817), AITER hipBLASLt GEMM online tuning (#40426), `permute_cols` for ROCm (#44674), blocks-first KV layout for AMD (#43660), N=5 wvSplitK for spec decode (#40687), MoRI connector improvements (#43303, #41751, #40344).
* **Intel XPU**: `block_fp8_moe` (#42139), block-scaled W8A8 FP8 path (#39968), WNA16 oracle for GPTQ sym-int4 (#41426), rms_norm/act quant fusions (#43963), GDN-attention MTP (#43565), Triton selective-scan op (#43421), fused-MoE LoRA kernel crash fix (#43646), transparent sleep mode (#37149), CPU/tiering offloading on XPU (#36423), DeepSeek-V4 attention decode path (#42953).
* **CPU & other architectures**: zentorch-accelerated W8A8/W4A16 on AMD Zen CPUs (#41813), CPU top-k/top-p Triton sampling (#43633), non-divisible GQA decode in mixed batches (#43032), `cpu_awq` folded into `awq_marlin` (#43841), RISC-V RVV WNA16 helpers (#42730), fused GDN gated-delta-rule kernels (#43534), PowerPC SHM communicator (#43754), arm64 CI image (#41303).
* **TPU**: tpu-inference upgraded to v0.20.0 (#43394) then v0.21.0 (#44621).
* **torch stable ABI**: Continued migration of kernels to the libtorch stable ABI — merge_attn_states/mamba/sampler [8/n] (#43361), attention/cache kernels [9/n] (#43717), header files (#44013), cuda_view/silu_and_mul [10/n] (#44334), custom all-reduce/DeepSeek-V4 fused MLA/MXFP8 MoE [10b/n] (#44365); ROCm fallback to regular ABI (#44648), `_has_module` trial-import verification (#44035).

### Quantization
* **ModelOpt**: LM-head quantization (#42124), MXFP8 non-gated MoE (#42958).
* **compressed-tensors**: WNA8O8Int linears and WNInt embeddings (#44340), asymmetric MoE WNA16 Marlin (#44025), single-class NVFP4 linear refactor (#42443).
* **Kernels & backends**: Triton W4A16 as CUDA fallback for non-Marlin-aligned shapes (#43731), Marlin MoE on SM 12.x (#40923), fail-fast for unsupported NVFP4 KV-cache-dtype arch (#43669), CuteDSL compressor 128-split kernel optimization (#44230), TRTLLM NVFP4 MoE chunking fix (#43599), `routed_scaling_factor` passed to FlashInfer TRTLLM BF16 MoE (#43769).
* **MoE refactor (oracle)**: Migrated ModelOpt MXFP8 (#42768), W4A8-int8 (#42789), and WNA16 backend selection (#42553) into the modular-kernel oracle; removed `supports_expert_map` (#43108) and the inplace fused-experts mechanism (#43727).

### API & Frontend
* **Anthropic Messages API**: Structured output and effort support (#42396), system-role messages inside the messages array (#44283).
* **OpenAI / Responses API**: `chat_template_kwargs` in Responses (#43761), `reasoning_effort` mapped to `enable_thinking` in chat-template kwargs (#43401), developer-to-system conversion in the HF renderer (#43590), unstreamed tool-call-args streaming fix (#44348).
* **Parsers**: Unified reasoning + tool-call parsing behind `Parser.parse()` (#44267), Responses parser migrated to the unified interface (#42977), unstreamed tool-arg flush moved into the parser (#44017); new/fixed tool parsers — MiniCPM5 XML (#43175), Qwen3 XML JSON-args-first (#43243), DeepSeek DSML incremental streaming (#42879), first-args-chunk serializer fix (#42683), `tool_choice="none"` honored in streaming (#42752), null-tool-args crash fix (#43862).
* **Frontend**: GPT-OSS instruction rendering (#44330), Harmony `stop_token_ids` cleanup (#44009), consistent `VLLMValidationError` in chat/completion validators (#36254), consolidation of dev entrypoints (#44170) and online-serving utils (#44479).
* **Rust frontend**: Streaming `generate` endpoint (#43779), dynamic LoRA endpoints (#43778), `/version` (#43854) and `/server_info` (#43942), server-router extension hook (#43774), `--enable-request-id-headers` (#43883), recursive tool-parameter conversion (#44299), `include_reasoning=false` (#44391), `--language-model-only` skips the multimodal processor (#44500), per-engine batch auto-abort (#44591), HF chat-template fixes (#44311), cross-DP aggregation of `is_sleeping`/`reset_prefix_cache` (#43429); new tool parsers — InternLM2 (#43481), hy_v3 (#43872), Phi-4-mini JSON (#44213), Gemma4 (#43850).
* **Benchmarks**: Timed trace replay for Moonshot/Alibaba workloads in `vllm bench serve` (#39795), reasoning-model (thinking) benchmarking via `--chat-template-kwargs` (#44244).

This release ships a coordinated security-hardening batch (much of it from security researcher @jperezdealgaba). Note: several of these fixes (#44321, #44680, #44744, #44970, #44971, #44974, #45113, #45116, #45119) merged to `main` as part of the disclosure and are not contained in the `v0.23.0` git tag itself; they are documented here as part of the release's security story.

* **Denial of service**: Fix remote DoS via invalid recovered-token reinjection in speculative decoding (#44744), and DoS via an audio decompression bomb in the speech-to-text endpoint (#44970).
* **Information disclosure**: Fix int32 truncation in the GGUF dequantize kernels (#44971).
* **Untrusted input / media handling**: Correct image EXIF orientation and tRNS transparency handling (#44974); reject out-of-vocabulary token IDs before they reach the GPU logprob path (#44042) and in Rust-frontend request params (#44680); reject non-finite `temperature`/`repetition_penalty` (#45116), invalid `thinking_token_budget` (#43402), non-positive `ParallelConfig` knobs (#44057), zero-valued config fields (#43794), and out-of-range `max_num_scheduled_tokens` (#44207).
* **Error-message hardening**: Apply `sanitize_message` to the Anthropic and STT error paths (#45119), and clarify the audio-duration-limit rejection error (#45113).
* **Transport & authentication**: SSL/TLS support for the data-parallel supervisor (#43688) and API-key authentication in the Rust frontend (#44321).
* **Robustness**: Fix a UTF-8 char-boundary panic in the Rust incremental detokenizer on malformed input (#44620).

### Dependencies
* FlashInfer v0.6.12 (#44036), ROCm 7.2.3 (#43136), AITER v0.1.13.post1 (#44265), tpu-inference v0.21.0 (#44621), CuTe DSL v4.5.2 (#43745), mistral-common bump (#44649), fastsafetensors v0.3.2 (#43625).
* Removed the stale cuDNN frontend upper bound (#42599); Docker fixes for flashinfer-jit-cache (#44366) and CUTLASS DSL cu13 install order (#45204).

### Deprecations
* Deprecate `JAISLMHeadModel` (#43784).
* Begin the deprecation cycle for the NixlConnector `kv_both` role (#43874).
* Remove functions previously scheduled for deprecation in v0.21.0 (#43358).

## New Contributors

* @aadwived made their first contribution in https://github.com/vllm-project/vllm/pull/41813
* @adhithyamulticoreware made their first contribution in https://github.com/vllm-project/vllm/pull/44615
* @adityasingh2400 made their first contribution in https://github.com/vllm-project/vllm/pull/43550
* @adotdad made their first contribution in https://github.com/vllm-project/vllm/pull/43100
* @amd-fuweiy made their first contribution in https://github.com/vllm-project/vllm/pull/43684
* @andakai made their first contribution in https://github.com/vllm-project/vllm/pull/43617
* @animeshtrivedi made their first contribution in https://github.com/vllm-project/vllm/pull/39795
* @BramVanroy made their first contribution in https://github.com/vllm-project/vllm/pull/43087
* @CienetStingLin made their first contribution in https://github.com/vllm-project/vllm/pull/43394
* @devin-lai made their first contribution in https://github.com/vllm-project/vllm/pull/44213
* @Dymasik made their first contribution in https://github.com/vllm-project/vllm/pull/43982
* @ECMGit made their first contribution in https://github.com/vllm-project/vllm/pull/43332
* @fallintoplace made their first contribution in https://github.com/vllm-project/vllm/pull/43540
* @gagandhakrey made their first contribution in https://github.com/vllm-project/vllm/pull/43792
* @galletas1712 made their first contribution in https://github.com/vllm-project/vllm/pull/43926
* @garrygale made their first contribution in https://github.com/vllm-project/vllm/pull/44205
* @Gruner-atero made their first contribution in https://github.com/vllm-project/vllm/pull/42967
* @hanlin12-AMD made their first contribution in https://github.com/vllm-project/vllm/pull/40426
* @harshaljanjani made their first contribution in https://github.com/vllm-project/vllm/pull/41459
* @Holworth made their first contribution in https://github.com/vllm-project/vllm/pull/39562
* @hoobnn made their first contribution in https://github.com/vllm-project/vllm/pull/42752
* @HueCodes made their first contribution in https://github.com/vllm-project/vllm/pull/44591
* @IdoAtadTD made their first contribution in https://github.com/vllm-project/vllm/pull/43978
* @jasonboukheir made their first contribution in https://github.com/vllm-project/vllm/pull/41426
* @Jie-Fang made their first contribution in https://github.com/vllm-project/vllm/pull/43584
* @JINO-ROHIT made their first contribution in https://github.com/vllm-project/vllm/pull/43830
* @JMonde made their first contribution in https://github.com/vllm-project/vllm/pull/37622
* @JohnQinAMD made their first contribution in https://github.com/vllm-project/vllm/pull/43331
* @jwzheng96 made their first contribution in https://github.com/vllm-project/vllm/pull/44057
* @Kartavyasonar made their first contribution in https://github.com/vllm-project/vllm/pull/43669
* @Krishnachaitanyakc made their first contribution in https://github.com/vllm-project/vllm/pull/38053
* @linzm1007 made their first contribution in https://github.com/vllm-project/vllm/pull/43402
* @MaciejBalaNV made their first contribution in https://github.com/vllm-project/vllm/pull/43356
* @Majid-Taheri made their first contribution in https://github.com/vllm-project/vllm/pull/43803
* @mfylcek made their first contribution in https://github.com/vllm-project/vllm/pull/43421
* @MHYangAMD made their first contribution in https://github.com/vllm-project/vllm/pull/42595
* @mikekg made their first contribution in https://github.com/vllm-project/vllm/pull/43330
* @nightcityblade made their first contribution in https://github.com/vllm-project/vllm/pull/44118
* @NolanHo made their first contribution in https://github.com/vllm-project/vllm/pull/43774
* @oguzhankir made their first contribution in https://github.com/vllm-project/vllm/pull/41759
* @okorzh-amd made their first contribution in https://github.com/vllm-project/vllm/pull/42129
* @Oxygen56 made their first contribution in https://github.com/vllm-project/vllm/pull/44236
* @QiliangCui2023 made their first contribution in https://github.com/vllm-project/vllm/pull/44476
* @rajkiranjoshi made their first contribution in https://github.com/vllm-project/vllm/pull/42083
* @Rukhaiya2004 made their first contribution in https://github.com/vllm-project/vllm/pull/43754
* @ruocco made their first contribution in https://github.com/vllm-project/vllm/pull/39983
* @sphinx07 made their first contribution in https://github.com/vllm-project/vllm/pull/43817
* @SunskyXH made their first contribution in https://github.com/vllm-project/vllm/pull/44215
* @ThibaultCastells made their first contribution in https://github.com/vllm-project/vllm/pull/43636
* @tianyu-z made their first contribution in https://github.com/vllm-project/vllm/pull/43150
* @tonyliu312 made their first contribution in https://github.com/vllm-project/vllm/pull/40923
* @tushar00jain made their first contribution in https://github.com/vllm-project/vllm/pull/41980
* @viiccwen made their first contribution in https://github.com/vllm-project/vllm/pull/44617
* @Vikrantpalle made their first contribution in https://github.com/vllm-project/vllm/pull/38804
* @wanghenshui made their first contribution in https://github.com/vllm-project/vllm/pull/44410
* @willamhou made their first contribution in https://github.com/vllm-project/vllm/pull/43429
* @william-rom made their first contribution in https://github.com/vllm-project/vllm/pull/43862
* @xiaozcy made their first contribution in https://github.com/vllm-project/vllm/pull/43843
* @XuZhou26 made their first contribution in https://github.com/vllm-project/vllm/pull/44618
* @Yadan-Wei made their first contribution in https://github.com/vllm-project/vllm/pull/44559
* @zhangtao2-1 made their first contribution in https://github.com/vllm-project/vllm/pull/43175
* @zvik made their first contribution in https://github.com/vllm-project/vllm/pull/43519
* @zzt93 made their first contribution in https://github.com/vllm-project/vllm/pull/43770

## Contributors

Thank you to everyone who made this release possible!

@AndreasKaratzas, @WoosukKwon, @hmellor, @BugenZhao, @yewentao256, @njhill, @khluu, @vadiklyutiy, @sfeng33, @bnellnm, @zyongye, @NickLucche, @JartX, @lucianommartins, @cleonard530, @wzhao18, @yma11, @simondanielsson, @chaojun-zhang, @mmangkad, @jeejeelee, @chaunceyjiang, @bigPYJ1151, @ronensc, @taneem-ibrahim, @gau-nernst, @LucasWilkinson, @MatthewBonanni, @chunyang-wen, @tjtanaa, @yzong-rh, @JaredforReal, @zixi-qi, @Isotr0py, @noooop, @Xunzhuo, @ivanium, @zufangzhu, @DaoyuanLi2816, @CienetStingLin, @Jie-Fang, @aoshen02, @akii96, @benchislett, @MengqingCao, @rshavitt, @kliuae, @omerpaz95, @willamhou, @Majid-Taheri, @micah-wil, @ricky-chaoju, @mikekg, @mgoin, @mayuyuace, @Etelis, @ilmarkov, @tlrmchlsmth, @UranusSeven, @bedeks, @izhuhaoran, @ZJY0516, @fadara01, @pschlan-amd, @dependabot[bot], @wangxiyuan, @Oxygen56, @charlifu, @varun-sundar-rabindranath, @shen-shanshan, @TheEpicDolphin, @adobrzyn, @XuZhou26, @Terrencezzj, @zhejiangxiaomai, @ILikeIneine, @yubofredwang, @chfeng-cs, @ThibaultCastells, @linzm1007, @javierdejesusda, @meenchen, @zhewenl, @xyang16, @angelayi, @nholmber, @zhangtao2-1, @adityasingh2400, @ashwing, @sts07142, @jatseng-ai, @fallintoplace, @andakai, @amitz-nv, @bbartels, @he-yufeng, @ignaciosica, @JINO-ROHIT, @tonyliu312, @QwertyJack, @animeshtrivedi, @jzakrzew, @juliendenize, @zexplorerhj, @ruocco, @mgehre-amd, @jasonboukheir, @MaciejBalaNV, @JohnQinAMD, @huanghua1994, @rajkiranjoshi, @rasmith, @harshaljanjani, @ltd0924, @wdhongtw, @yintong-lu, @tianmu-li, @jikunshang, @JMonde, @MHYangAMD, @frida-andersson, @Wauplin, @czhu-cohere, @gagandhakrey, @nemanjaudovic, @Liangliang-Ma, @liulanze, @sphinx07, @aadwived, @nightcityblade, @umut-polat, @jeffreywang88, @wcynb1023, @zzt93, @shadeMe, @Dao007forever, @alec-flowers, @Krishnachaitanyakc, @orozery, @BWAAEEEK, @cinnamonica02, @albertoperdomo2, @Rukhaiya2004, @mfylcek, @shreyas269, @Gruner-atero, @TomerBN-Nvidia, @wjinxu, @IdoAtadTD, @xiaozcy, @brian-dellabetta, @zhenwei-intel, @adotdad, @Kartavyasonar, @lesj0610, @ECMGit, @cakeng, @william-rom, @qiching, @NolanHo, @andylolu2, @xwu-intel, @linitra24, @hoobnn, @Dymasik, @wanghenshui, @maobaolong, @oguzhankir, @okorzh-amd, @Kevin-XiongC, @jiahanc, @garrygale, @dsikka, @QiliangCui2023, @wjabbour, @zvik, @tc-mb, @jwzheng96, @divakar-amd, @tushar00jain, @galletas1712, @hanlin12-AMD, @tuukkjs, @viiccwen, @Sunt-ing, @HueCodes, @tianyu-z, @adhithyamulticoreware, @rishitdholakia13, @effi-ofer, @Vikrantpalle, @walterbm, @devin-lai, @Yadan-Wei, @amd-fuweiy, @maeehart, @qyYue1389, @BramVanroy, @SunskyXH, @Holworth, @majian4work, @xaguilar-amd, @Rohan138
