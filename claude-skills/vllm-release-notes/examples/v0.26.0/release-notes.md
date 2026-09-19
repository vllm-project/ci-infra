# vLLM v0.26.0 Release Notes

## Highlights

This release features 411 commits from 212 contributors (61 new)!

* **New Inkling model family** with a full support stack: base modeling (#48799), piecewise CUDA graph support (#48822), Hopper FA4 relative attention (#48858), MTP=1 speculative decoding (#48869), LoRA (#48884), and standard ModelOpt NVFP4 quantization (#48990).
* **DeepSeek-V4 performance push** across vendors: a specialized routing kernel (2.94% E2E TPOT, #48660), `fused_topk_bias` (1.5–2x kernel, #47463), and redundant repeat/copy removal (1.8% E2E TPOT, #48137), plus ROCm two-stage compressor for HCA prefill (#47718), sparse decode/prefill optimizations (#48519, #48788, #46275), and DSpark speculative decoding on AMD (#47419) and XPU (#47677).
* **fp32 `lm_head` for generation models via `head_dtype`** (#48390), extended to the LoRA path (#48525) and given a ROCm `torch.mm` fast path (#48688), improving accuracy for generation heads.
* **Flexible attention backends**: the attention backend can now be selected per KV-cache group (#48012), and sliding-window support is now an explicit backend capability (#48011) — improving support for hybrid models.
* **KV offloading & tiered secondary storage** matured substantially: offloading metrics (#45958, #47666, #47679), tier-owned event handling (#46544, #47923), object-store secondary tier with workload identity (#47063, #47274, #48150), DP-replica-aware tiering (#47987), and encoder-cache (EC) connectors including CPU offloading (#42433, #47423).
* **Rust frontend** gained multimodal video (#47959) and audio (#48554), a Seed-OSS tool parser (#47741), and a native `vllm-bench` port (#48107).
* **Transformers 5.13.0** (#47867) with more models migrated to the Transformers modeling backend: Olmo/Olmo2 (#48100), MistralLarge3 (#48153), and HunyuanVL (#47872).

### Model Support
* New models: Inkling family (#48799, #48822, #48858, #48869, #48884, #48990), BertForMaskedLM (#48463), RobertaForTokenClassification / XLMRobertaForTokenClassification (#47991), LongCat-Flash-Lite n-gram embedding (#47857), Cosmos3 Edge Reasoner (#48291) and Cosmos3-Super registration (#48211), TranslateGemma-12b-it (#41599).
* Transformers backend migrations: Olmo/Olmo2 (#48100), MistralLarge3 to AutoWeightsLoader (#48153), HunyuanVL native transformers processor for transformers 5.13 (#47872).
* GLM5.2: migrate MoE sequence-parallel support to the non-torch-compiled path (#47881).
* LoRA: FlashInfer MoE LoRA for BF16 models (#48632), LoRA for tower/connector in LlavaNextVideo (#48594), fp32 `lm_head` on the LoRA path (#48525), optimized `TrtLlmLoRAExperts` (#48759).
* Multimodal: automatic fallback to ViT data parallelism when TP is unavailable (#49046).
* Fixes: correct pooling scores for chunked prefill under `torch.compile` (#48901).

### Engine Core
* fp32 `lm_head` for generation models via `head_dtype` (#48390); lower memory for capturing large CUDA graph sizes (#48483); opt-in persistence and reuse of the memory-profiling result across boots (#47388); improved InstantTensor loading (#46868).
* Attention: select a different attention backend per KV-cache group (#48012); sliding-window as an explicit backend capability (#48011); KV-cache layout refactor packing K/V into the content dim across backends (#44455); MRV2 virtual-batch PCP for MLA (#46570).
* Speculative decoding: runtime draft weight update (#46725), hybrid (SWA + full attention) DFlash drafters (#47914), SWA support for qwen-eagle3 (#47568), Gemma4-12B DSpark draft model (#47216), DSv4 DSpark on AMD (#47419), separate `kv_cache_dtype` for `speculative_config` (#48787).
* KV offloading: basic offloading metrics (#45958), split CPU cache usage into read/write gauges (#47666) and tiering-lookup-delay into sync/async histograms (#47679), tier-owned event handling and BlockStored events (#46544, #47923), object-store secondary tier with workload identity (#47063, #47274, #48150), DP-replica-aware tiering (#47987), `blocks_per_chunk` config for heterogeneous KV groups (#48878), P2P default host/port env vars (#47636).
* Caching: partial prefix-cache hit for hybrid models (#46384), selective hybrid cache retention (#47782), report prefix-cache-reused blocks in full report mode (#45261).
* Reasoning: optimize TPOT for thinking budget when used with speculative decoding (#46662).
* RLHF: stateful trainer-send abstractions (#48042).
* Fixes: host memory leak from undrained `new_block_ids` (#44490), DSv3.2 + MTP + sequence-parallel accuracy (#48036).

### Hardware & Performance
* DeepSeek-V4: specialized routing kernel (2.94% E2E TPOT, #48660), `fused_topk_bias` 1.5–2x (#47463), redundant repeat/copy removal (1.8% TPOT, #48137).
* MoE router GEMMs: BF16x3 router GEMM (#47973), FP32 router GEMV (#48335), generic CuteDSL LL BF16 router GEMM (#42562); TRTLLM BF16 MoE modular kernel (#45182); write FlashInfer combine into final output (#47156).
* Qwen: fuse more RMSNorm + all-reduce in Qwen3.5 (#46998), replace MoE all-reduce with reduce-scatter (#47006), Qwen3.5 H20 optimization (#48350), expand Triton warmup coverage (#47546).
* MLA: dense MHA path for short sparse-MLA sequences (#47327); MiniMax-M3 long-context decode indexer on sm100 (#48582).
* Kernels: CUDA kernel for ReLUSquaredActivation / relu^2 (#39058), Helion kernel lazy registration (#48264), vectorize `_copy_mamba_state_block` to uint64 (#48110), stop upcasting logits to fp32 in the sampler (#48641).
* ROCm: fp32 `head_dtype` `torch.mm` fast path (#48688), DSv4 two-stage compressor kernel (#47718), sparse decode/prefill optimizations (#48519, #48788, #46275), DSv3.2 sparse MLA KV-split heuristic (#46832) and MTP CUDA-graph mode (#45149), MXFP8 GEMM for MiniMax-M3 (#46117), AITER sparse paged attention + spec decode for MiniMax-M3 (#47287, #47984), MiniMax-M2 fused QK-norm + all-reduce via AITER (#44849), HybridW4A16 linear kernel (#40977), Qwen3-30B-A3B QK-Norm+RoPE+KV runtime fusion (#42749).
* XPU: batch-invariant kernels (#41934), HND KV layout support (#47975), DSpark spec decode for DSv4 (#47677), nightly/release image publishing (#47880, #48126).
* CPU: DFlash speculative decoding for GDN models on CPU (#46090), s390x NUMA topology (#40714), native macOS arm64 CPU wheel builds (#48289); POWER VSX math function optimization (#47321) and IBM Power docker builds using prebuilt wheels (#46017).
* Distributed fusion: FlashInfer MNNVL all-reduce RMS quant fusion (#48064).
* Build/autotune: arm64 Blackwell SM10x/SM110 image builds (#48041); skip CuTeDSL fp4_gemm autotuning by default (#48268).

### Large Scale Serving & Distributed
* Decode Context Parallel (DCP): hybrid attention support (#40996), DCP + Eagle for Tokenspeed MLA backends (#48180).
* PD disaggregation: NIXL pipeline-parallel prefill in push mode (#45880).
* Encoder-cache connectors: EC transfer params (#42433) and CPU-offloading EC connector (#47423).

### Quantization
* Humming w[2-7]a[4,8] weight-only inference with compressed-tensors (#46390); int4 quantization for the emulation MoE backend (#48451); INT2 XPU weight-only quant linear (#47521).
* NVFP4/MXFP4: `nvfp4_per_token` online MoE quantization (#48538), CuTe-DSL FlashInfer MXFP4 quantization (#48417); bounded peak memory when repacking FP4 MoE weights for Marlin (#47851) and for NVFP4 MoE weight loading (#46276).
* MLA: `kv_cache_dtype_skip_layers` support (#47309).
* ROCm: HybridW4A16 linear kernel (#40977).

### API & Frontend
* Rust frontend: multimodal video (#47959) and audio (#48554), Seed-OSS tool parser (#47741), native `vllm-bench` port (#48107), `continue_final_message` handling with renderer sentinel (#47844).
* OpenAI compatibility: `bad_words` in `/v1/completions` (#46793), expose `logprob_token_ids` on Python OpenAI endpoints (#43463), `include_reasoning` param for non-Harmony models (#44301), populate `num_cache_creation_tokens` on Messages responses (#48535).
* Endpoint plugins framework (#47454); `/abort_requests` on the RLHF dev API router (#47173); Deepstream video decoding backend (#42424); overlap preprocessing and computation for pooling models in offline inference (#47699).
* UX: human-readable integers for more CLI args (#47608), CuTeDSL compilation progress bar (#48881), expanded GPU profiler config scope/annotations (#37524), log worker exit code when a process dies unexpectedly (#38641).
* Stability/correctness: handle grammar compilation failures without crashing the engine (#47312), fix logprobs token-string collision from SentencePiece spaces (#48674).

### Security
* Replace diskcache to eliminate pickle deserialization (#44549).
* Fix a concurrent sparse-invariant race that bypassed CVE remediation (#48583).
* Add resource-bounds validation to derender endpoints (#47260); sanitize server file paths from validation error responses (#46415); bound the completion prompt list to prevent unbounded engine fan-out (#47845); guard lm-format-enforcer regex compilation with a timeout (#47595).

### Dependencies
* Transformers 5.13.0 (#47867), FlashInfer 0.6.14 (#47669), NIXL 1.3.1 (#47559), tpu-inference v0.24.0 (#47835), nvidia-cutlass-dsl 4.6.0 (#47442), vllm_xpu_kernels v0.1.11.1 (#48942).
* FlashAttention 3 pinned to the torch stable-ABI commit (#47995); ABI-stable FlashMLA build (#48174).

### Deprecations & Removals
* Models removed: TeleChat (#47989), Persimmon and Fuyu (#48096).

## New Contributors

* @adhi29 made their first contribution in https://github.com/vllm-project/vllm/pull/48262
* @adsridhar made their first contribution in https://github.com/vllm-project/vllm/pull/48291
* @alexxu-roblox made their first contribution in https://github.com/vllm-project/vllm/pull/48025
* @amd-ethany made their first contribution in https://github.com/vllm-project/vllm/pull/46117
* @AndyDai-nv made their first contribution in https://github.com/vllm-project/vllm/pull/48507
* @aoright made their first contribution in https://github.com/vllm-project/vllm/pull/47797
* @ap9272 made their first contribution in https://github.com/vllm-project/vllm/pull/43896
* @avalliappan-nvidia made their first contribution in https://github.com/vllm-project/vllm/pull/47460
* @brijrajk made their first contribution in https://github.com/vllm-project/vllm/pull/44349
* @Debasish-87 made their first contribution in https://github.com/vllm-project/vllm/pull/48878
* @devalshahamd made their first contribution in https://github.com/vllm-project/vllm/pull/37524
* @DiegoCao made their first contribution in https://github.com/vllm-project/vllm/pull/47216
* @drakosha made their first contribution in https://github.com/vllm-project/vllm/pull/46972
* @edwinlim0919 made their first contribution in https://github.com/vllm-project/vllm/pull/47495
* @ErenAta16 made their first contribution in https://github.com/vllm-project/vllm/pull/48333
* @Functionhx made their first contribution in https://github.com/vllm-project/vllm/pull/48153
* @gangula-karthik made their first contribution in https://github.com/vllm-project/vllm/pull/48594
* @Gavin-Morris-04 made their first contribution in https://github.com/vllm-project/vllm/pull/48293
* @giuseppegrossi made their first contribution in https://github.com/vllm-project/vllm/pull/48159
* @GongLei-HW made their first contribution in https://github.com/vllm-project/vllm/pull/45261
* @guoriyue made their first contribution in https://github.com/vllm-project/vllm/pull/48211
* @hugo-cen made their first contribution in https://github.com/vllm-project/vllm/pull/48330
* @ibondarenko1 made their first contribution in https://github.com/vllm-project/vllm/pull/43117
* @iyastreb made their first contribution in https://github.com/vllm-project/vllm/pull/48209
* @jacklin78911-collab made their first contribution in https://github.com/vllm-project/vllm/pull/47744
* @jhu960213 made their first contribution in https://github.com/vllm-project/vllm/pull/42749
* @joanvelja made their first contribution in https://github.com/vllm-project/vllm/pull/44371
* @KKothuri made their first contribution in https://github.com/vllm-project/vllm/pull/48390
* @kl527 made their first contribution in https://github.com/vllm-project/vllm/pull/47464
* @krishy91 made their first contribution in https://github.com/vllm-project/vllm/pull/47991
* @langzhao-netizen made their first contribution in https://github.com/vllm-project/vllm/pull/43463
* @lishunyang12 made their first contribution in https://github.com/vllm-project/vllm/pull/49015
* @mahadrehmann made their first contribution in https://github.com/vllm-project/vllm/pull/48098
* @ManaEstras made their first contribution in https://github.com/vllm-project/vllm/pull/47872
* @mosya415 made their first contribution in https://github.com/vllm-project/vllm/pull/48846
* @mwoodson made their first contribution in https://github.com/vllm-project/vllm/pull/48523
* @MynameFelix made their first contribution in https://github.com/vllm-project/vllm/pull/41811
* @nicklasfrahm made their first contribution in https://github.com/vllm-project/vllm/pull/45313
* @passtoor-agi made their first contribution in https://github.com/vllm-project/vllm/pull/48849
* @pierDipi made their first contribution in https://github.com/vllm-project/vllm/pull/47063
* @ruikangliu made their first contribution in https://github.com/vllm-project/vllm/pull/47309
* @Sahil170595 made their first contribution in https://github.com/vllm-project/vllm/pull/45207
* @samnordmann made their first contribution in https://github.com/vllm-project/vllm/pull/47156
* @shawntsai made their first contribution in https://github.com/vllm-project/vllm/pull/47801
* @sungbin1015 made their first contribution in https://github.com/vllm-project/vllm/pull/46793
* @tanish-malekar made their first contribution in https://github.com/vllm-project/vllm/pull/39058
* @tomerg-nvidia made their first contribution in https://github.com/vllm-project/vllm/pull/47021
* @tsvikas made their first contribution in https://github.com/vllm-project/vllm/pull/47296
* @vanshbhatia-amd made their first contribution in https://github.com/vllm-project/vllm/pull/47767
* @ViranjanPagar made their first contribution in https://github.com/vllm-project/vllm/pull/42424
* @vivek8123 made their first contribution in https://github.com/vllm-project/vllm/pull/46017
* @vx120 made their first contribution in https://github.com/vllm-project/vllm/pull/46725
* @wangxingda made their first contribution in https://github.com/vllm-project/vllm/pull/48829
* @wendadawen made their first contribution in https://github.com/vllm-project/vllm/pull/46213
* @wenpengw-nv made their first contribution in https://github.com/vllm-project/vllm/pull/44863
* @woosebastian made their first contribution in https://github.com/vllm-project/vllm/pull/48901
* @Yancey0623 made their first contribution in https://github.com/vllm-project/vllm/pull/40996
* @yushangdi made their first contribution in https://github.com/vllm-project/vllm/pull/48868
* @yuvalluria made their first contribution in https://github.com/vllm-project/vllm/pull/46396
* @zihaomu made their first contribution in https://github.com/vllm-project/vllm/pull/47404
* @zqzten made their first contribution in https://github.com/vllm-project/vllm/pull/44303

## Contributors

@mgoin, @yewentao256, @NickLucche, @njhill, @LucasWilkinson, @micah-wil, @khluu, @AndreasKaratzas, @WoosukKwon, @aoshen02, @BugenZhao, @vanshbhatia-amd, @jperezdealgaba, @jeejeelee, @hmellor, @tlrmchlsmth, @MatthewBonanni, @Change72, @chaojun-zhang, @gau-nernst, @gnovack, @ZJY0516, @reidliu41, @Yejing-Lai, @taneem-ibrahim, @Srinivasoo7, @Sunt-ing, @AmeenP, @zhenwei-intel, @LopezCastroRoberto, @matteso1, @yzong-rh, @stefankoncarevic, @Isotr0py, @djramic, @charlifu, @peizhang56, @giuseppegrossi, @drakosha, @NickCao, @benchislett, @Rohan138, @hickeyma, @muhammadfawaz1, @rasmith, @zixi-qi, @music-dino, @BWAAEEEK, @xianbaoqian, @wendyliu235, @atalman, @joerowell, @ErenAta16, @AlejandroParedesLT, @gcanlin, @liranschour, @omerpaz95, @Alex-ai-future, @tanpinsiang, @Fangzhou-Ai, @KKothuri, @zxd1997066, @akii96, @nemanjaudovic, @Etelis, @afierka-intel, @DaoyuanLi2816, @HDCharles, @tahsintunan, @xiaohongchen1991, @sagearc, @mikekg, @kliuae, @qli88, @arpera, @yushangdi, @edwinlim0919, @tjtanaa, @ariG23498, @lucifer1004, @netanel-haber, @kl527, @Rukhaiya2004, @ronensc, @guan404ming, @shaunkotek, @liulanze, @pierDipi, @eldarkurtic, @simon-mo, @robinguo23, @CienetStingLin, @RishabhSaini, @Sahil170595, @jasonlizhengjian, @jacklin78911-collab, @walterbm, @amd-ethany, @zqzten, @nicklasfrahm, @hongxiayang, @alexeldeib, @ManaEstras, @sungbin1015, @aoright, @Saddss, @vivek8123, @voipmonitor, @shawntsai, @almayne, @ilmarkov, @cleonard530, @kjiang249, @chaunceyjiang, @bigPYJ1151, @tsvikas, @deng451e, @tvirolai-amd, @zhewenl, @zihaomu, @ap9272, @staugust, @Yancey0623, @GongLei-HW, @albertoperdomo2, @guoriyue, @ViranjanPagar, @Functionhx, @XuZhou26, @MynameFelix, @larryli2-amd, @ashwing, @thisisjimmyfb, @robertgshaw2-redhat, @mayuyuace, @ibondarenko1, @zhejiangxiaomai, @vx120, @hugo-cen, @tanish-malekar, @zzt93, @guybd, @R3hankhan123, @mmangkad, @omera-nv, @yma11, @Gavin-Morris-04, @pavanimajety, @shanjiaz, @wenpengw-nv, @atalhens, @langzhao-netizen, @emricksini-h, @Zhenzhong1, @DanBlanaru, @mgehre-amd, @mwoodson, @wangxiyuan, @adsridhar, @hnt2601, @gangula-karthik, @tomerg-nvidia, @adhi29, @rishitdholakia13, @divakar-amd, @chaeminlim-mb, @joanvelja, @russellb, @janeyx99, @aarushjain29, @wjabbour, @mahadrehmann, @krishy91, @tzielinski-habana, @avalliappan-nvidia, @ruikangliu, @majian4work, @maxyanghu, @brijrajk, @lengrongfu, @Josephasafg, @elvircrn, @xiaguan, @ricky-chaoju, @iyastreb, @ovidiusm, @tuukkjs, @noooop, @samnordmann, @AndyDai-nv, @xiao-llm, @DiegoCao, @yuvalluria, @jhu960213, @woosebastian, @Debasish-87, @esmeetu, @hao-aaron, @zhangj1an, @wendadawen, @juliendenize, @passtoor-agi, @mosya415, @labAxiaoming, @devalshahamd, @wangxingda, @xuebwang-amd, @fuscof-ibm, @alexxu-roblox, @frida-andersson, @lishunyang12, @izhuhaoran
