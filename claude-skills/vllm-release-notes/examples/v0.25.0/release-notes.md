# vLLM v0.25.0 Release Notes

## Highlights

This release features 558 commits from 232 contributors (64 new)!

* **Model Runner V2 is now the default for all dense models** (#44443). Building on quantized-model support from the previous release, MRv2 is now the standard execution path, with new support for EVS (#46535), realtime embeddings (#46762), prefix caching for Mamba hybrid models (#42406), multimodal-prefix bidirectional attention (#46942), and dynamic speculative decoding compatible with full CUDA graphs (#45953).
* **PagedAttention has been removed** (#47361). The legacy attention implementation is deleted now that V1/MRv2 backends are the standard path.
* **The Transformers modeling backend is now as fast as native vLLM** (#47187), and gained FP8 MoE support (#46820), CUDA graph + embed scaling fixes (#48010), and migration of GPTBigCode/Starcoder2 (#30966) and RoBERTa (#47452).
* **New models**: LLaVA-OneVision-2 (#44785), Unlimited OCR (#46564, #47102), MOSS-Transcribe-Diarize (#47729), openai/privacy-filter (#41026), and Hy3 (#47192). GLM-5 / DeepSeek-V3.2 landed in the model zoo (#46808) with GLM-5.2 tuning, and MiniMax-M3 gained pipeline parallelism (#45810) and NVFP4 support (#46756).
* **New Streaming Parser Engine** (#46610) — a unified tool-call/reasoning parsing framework, with a new Kimi k2.5/k2.6/k2.7 parser and ports of seed_oss (#46314) and DeepSeek V4 (#45877). The Rust frontend continues to mature with HTTPS/mTLS (#45890), a DP supervisor (#47076), and profiler control routes (#46306).
* **Universal speculative decoding for heterogeneous vocabularies (TLI)** (#38174), plus new DSpark (#46995) and DFlash (#46770, #46853) drafters.

### Model Support
* New models: LLaVA-OneVision-2 (#44785), Unlimited OCR (#46564) with a Triton R-SWA backend (#47102), MOSS-Transcribe-Diarize (#47729), openai/privacy-filter (#41026), Hy3 with token-suffix and JSON Schema array support (#47192).
* GLM-5 family: GLM-5 / DeepSeek-V3.2 added to the model zoo (#46808), GLM-5.2 FP32 gate (#47410), GLM MTP post-final-norm fix (#47448), GLM4V startup fix (#47155).
* MiniMax-M3: pipeline parallelism (#45810), streaming reasoning parsing (#45718), and `tok_sparse_select` from MSA replacing Triton kernels (#47502).
* Transformers backend: now as fast as native vLLM (#47187), FP8 MoE fix (#46820), embed scaling + CUDA graph fix (#48010), GPTBigCode/Starcoder2 (#30966) and RoBERTa (#47452) migration, M-RoPE `mm_token_type_ids` fix (#46552), tied-embedding `lm_head.bias` fix (#46835).
* Voxtral: migrated to mistral-common 1.11.5 audio API (#46705) and realtime token-feedback hang fix (#44461).
* Gemma family: Gemma4 sliding-window/FA4 attention fixes (#47217, #47332), Gemma4 MTP quant_config fix (#47091); DiffusionGemma tensor parallelism (#45719) and HF stability-window semantics (#45965).
* Other fixes: MiniCPM-V 4.6 language-backbone LoRA (#46740) and placeholder grid fix (#45918), pooled Whisper sliding-window sizing (#47071, #47437), Mamba/Mamba2 checkpoint-without-`architectures` crash fix (#46037), DeepSeek-V2 hidden-size and aux-hidden-state fixes (#46986, #46973).

### Engine Core
* Model Runner V2: default for all dense models (#44443); EVS (#46535), realtime embeddings (#46762), Mamba hybrid prefix caching (#42406), multimodal-prefix bidirectional attention (#46942), cross-attention warmup/block-table fixes (#46753, #47308), Mamba2 crash fix (#47428), scheduling slot accounting (#46974), model-ref cleanup on shutdown (#47483), bounded memory for large-logprobs requests (#46746).
* Speculative decoding: universal spec decode for heterogeneous vocabularies (TLI) (#38174); DSpark drafter + speculators checkpoint support (#46995, #47093); DFlash backend selection (#46770), per-layer RMSNorm fusion (#46761), CPU support (#44029), SWA+DFlash for MiMo (#46104), Laguna XS.2.1 drafter (#46853); MTP for Bailing hybrid models (#44880); block verification for rejection sampling (#46781); reduced TP communication for draft tokens (#46448).
* Sleep mode: pluggable sleep-mode backend abstraction (RFC #34303, #44074) with communicator-agnostic capability flags (#47243).
* Attention: FlashAttention block-size restriction removed for hybrid models (#36701), `FLASH_ATTN_MLA_SPARSE` Hopper sparse-MLA backend (#46189), DCP + FP8 KV cache in MLA decode (#44044), XQA decode kernels (#43232).
* KV offloading: tiering metric plumbing (#45959), request lifecycle fix (#46284), batched lookup in C (#46713), `LookupResult` enum (#46363).
* Misc: `VLLM_GPU_SYNC_CHECK` env var (#44800), VRAM semaphore infrastructure (#44465), skip detokenization in online beam search (#46422), several int32-overflow fixes in sampler/attention kernels (#46560, #47383, #47671).

### Hardware & Performance
* GLM-5.2 / DeepSeek: `fused_indexer_q_rope_quant` Triton kernel (1.9–3.3% E2E throughput) (#46862), reduce-scatter MoE all-reduce (3.1–3.2% E2E) (#46635), op fusion for GLM5/DSV3.2 (#46876), `token_to_req_indices` cache for DSv4 (5–6x kernel speedup) (#47474), better DSv4 MXFP8 kernel (#47229), redundant-op removal (#47198, #46651).
* NVIDIA/Blackwell: FlashInfer fused all-reduce tuned for world_size=16 on GB300 (#46392), restored NVFP4 swizzled-scale zero-init to recover Blackwell decode throughput (#45739), CuTeDSL/FA4-MLA warmup infrastructure (#46182), skip cooperative top-K on SM120 (#47164), B12x backend for non-gated MoEs (#43328).
* Kernels: Helion `fused_qk_norm_rope` (#44010) and `silu_and_mul_per_block_quant` (#43994), Triton MLA logits workspace (#46819), swap-AB optimization for fused MoE (#36559), vectorized fp32 `moe_sum` supporting any top-k (#46643), blocking CUDA events to avoid busy-polling the driver lock (#47081).
* AMD/ROCm: moved to torch 2.11 stable ABI (#47128); AITER FlashAttention MLA prefill backend `ROCM_AITER_FA` (#45033); fused shared-expert for GLM-4.5/6/7 (#44313) and MiniMax-M3 (#46474, #46545); AITER MoE optimization for DeepSeek-V4 (#46122); AITER custom all-reduce in CudaCommunicator (#46065); INT3 quantization for quickreduce (#45666).
* Intel XPU: W8A8 FP8 linear kernel with multi-granularity quant (#43645), pipeline-parallel accuracy fix (#47253), uniform-batch CUDA graph for FA2 (#46555), route mm_prefix models to Triton attention (#47688), C++ `get_memory_info` (#47134).
* CPU: accelerated unquantized MoE for AArch64 (#46353), macOS/Apple Silicon hang fix via OpenMP (#46769) and broken-install fix (#47457), compressed-tensor w8a8 int8 MoE (#42920), Mamba ShortConv (#35059), chunked prefill + prefix caching for Qwen3.5 (#46202), faster gelu via tanh AOR (#44639).
* RISC-V: RVV path for W4A8 INT4 GEMM (#45269), BF16 on VLEN=256 hardware (#45243), reduced LMUL pressure in INT4 LUT dequant (#47538). POWER: fp16 support on PowerPC (#46135).
* Platform: accelerator-agnostic `get_memory_info` (#44825).

### Large Scale Serving & Distributed
* Sequence parallelism without requiring DP, 1.9–5.0% E2E throughput improvement (#47070).
* Distributed: NCCL symmetric memory extended to AllGather and ReduceScatter (#46703), FlashInfer all-reduce defaults to MNNVL on single node (#47219, #47589), fault-tolerance backend to detect all2all peer faults and prevent corrupted output (#43637).
* Data parallel: throttle prefills based on local prefill work (#46532), rotate load-balancer tie-break to avoid engine bias (#47420), DP supervisor via the Rust frontend (#47076), DP MTP hang fix (#40589).
* PD disaggregation: secondary-tier implementation (#42285), Mooncake connector GDN (Qwen3.5) + MLA (DeepSeek-V4-Flash) support (#46807), NIXL Mamba1 support (#45019), MultiConnector `kv_transfer_params` merging (#46777), usage field exposed for disaggregated serving (#42748).
* DCP: FlashInfer MLA support (#43729), FLASHINFER_MLA_SPARSE support (#46076), LSE log-base fixes (#47079); Mooncake parallelized KV load (#45971) and DCP>1 lookup fix (#46855).
* ROCm: stabilized high-throughput DBO for DP+EP (#46990), EPLB for Quark OCP MXFP4 MoE (#47220).

### Quantization
* 2/3/5/6/7-bit pack-quantized weight-only inference (Humming) (#46389), Triton INT4 per-token-head KV cache quantization (#40835).
* NVFP4: fused weight dequantization with compute in the MoE MLP Triton kernel (#44667), NVFP4 KV cache with skip-layers sliding window (#42890), MiniMax-M3 ModelOpt NVFP4 support (#46756).
* FP8: weights padding for per-block online quantization (#44763); deprecated the old FP8 online MoE quantization class (#44514).
* Marlin: thread-tile padding extended to MoE (WNA16 + FP8/MXFP8) (#45703), int8 grouped WNA16 MoE (#47154); FlashInfer MXINT4 MoE for gated SiLU (#46518).
* Fixes: W8A8 int-quant scheme-selection regression (#46860), tied quantized embeddings for ModelOpt Gemma4 (#45544), NVFP4+MTP crash on Qwen3Next (#46316), ModelOpt mixed-precision for sparse configs (#47318), CPU w4a8_int8 MoE path (#46739), actionable error on group-size/TP mismatch (#46230).

### API & Frontend
* Streaming Parser Engine (#46610): unified tool-call/reasoning parsing with a new Kimi k2.5/k2.6/k2.7 parser; ported seed_oss (#46314) and DeepSeek V4 (#45877).
* OpenAI compatibility: Responses API namespace tools (#47024), per-request timing `metrics` field on Chat/Completions responses (#46768), token offsets on render endpoints (#44226), `return_loss_mask` for training-data generation (#46846), HTTP 422 for unprocessable image URLs (#47165).
* gpt-oss / Harmony: dedicated Harmony renderer (#46800), `process_eos()` flush (#46437), raw-output recovery on non-terminal parse (#47062, #47379).
* Rust frontend: static HTTPS and mTLS for HTTP and gRPC (#45890), DP supervisor (#47076), profiler control routes (#46306), `repetition_detection` sampling param (#46684), unified/combined parser interface (#46583), reduced multimodal tensor copies (#47581), plus many parser and validation fixes.
* Video: TorchCodec added as a video decoding backend (#46609).
* CLI/UX: TTFT and TPS printing in `vllm chat` (#46775), `model_class_overrides` for development/debugging (#47148).
* Tooling/validation: many tool-parser fixes (Kimi K2 IDs #46344, PoolsideV1 #46486/#47311, non-ASCII arguments #46308, `thinking_token_budget` re-entry #43757); rejection of invalid config values (#44070, #44002, #46612) and degenerate `structured_outputs` that crash EngineCore (#45346).

### Security
* Prevent image decompression-bomb OOM denial of service (#47010).
* Prevent an infinite loop in `split_audio` with NaN audio samples (#46463).
* Bound tokenizer work when an explicit `truncation_side` is set (#47007).
* Block request-level GPU video backend selection (#47259).
* Document the gRPC interface as insecure, for private use only (#45903).

### Dependencies
* FlashInfer 0.6.13 (#46683), tpu-inference v0.23.0 (#46568), aiter 0.1.16.post2 (#46692), vllm_xpu_kernels v0.1.10.1 (#46607), huggingface-hub v1.22.0 (#47551).
* DeepGEMM updated to enable SM120 support (#47304), FlashAttention 3 built against the torch stable API (#46644), Rust frontend TLS switched from rustls to native-tls/OpenSSL (#46696).

### Deprecations & Removals
* **PagedAttention deleted** (#47361).
* Models removed: Baichuan (#46362), Aquila (#46605), Grok (#46706), Tarsier / Tarsier2 (#47143), AyaVision / MusicFlamingo (#47263), Mantis (#46806).
* Deprecated the old FP8 online MoE quantization class (#44514); legacy `api_server.py` moved to the examples directory (#46783); `gptq_marlin` removed from supported ROCm quant schemes (#46655).

## New Contributors

* @aaarkai made their first contribution in https://github.com/vllm-project/vllm/pull/44610
* @Acaciasama made their first contribution in https://github.com/vllm-project/vllm/pull/45850
* @ACEEE-1222 made their first contribution in https://github.com/vllm-project/vllm/pull/47716
* @adamkbaranowski made their first contribution in https://github.com/vllm-project/vllm/pull/46853
* @AgenticSpark made their first contribution in https://github.com/vllm-project/vllm/pull/46071
* @AIvashov made their first contribution in https://github.com/vllm-project/vllm/pull/42748
* @akinsella made their first contribution in https://github.com/vllm-project/vllm/pull/47165
* @aldenlobo made their first contribution in https://github.com/vllm-project/vllm/pull/45961
* @alex101-ops made their first contribution in https://github.com/vllm-project/vllm/pull/44880
* @aman0603 made their first contribution in https://github.com/vllm-project/vllm/pull/46945
* @Aneureka made their first contribution in https://github.com/vllm-project/vllm/pull/46838
* @ArsalanShakil made their first contribution in https://github.com/vllm-project/vllm/pull/46236
* @ayush1399 made their first contribution in https://github.com/vllm-project/vllm/pull/47091
* @blasrodri made their first contribution in https://github.com/vllm-project/vllm/pull/46827
* @calvarado2004 made their first contribution in https://github.com/vllm-project/vllm/pull/46177
* @chengzheng345 made their first contribution in https://github.com/vllm-project/vllm/pull/44785
* @cpersson-amd made their first contribution in https://github.com/vllm-project/vllm/pull/47519
* @cyq1017 made their first contribution in https://github.com/vllm-project/vllm/pull/46101
* @davispuh made their first contribution in https://github.com/vllm-project/vllm/pull/35232
* @decarpentierg made their first contribution in https://github.com/vllm-project/vllm/pull/46552
* @eparshut made their first contribution in https://github.com/vllm-project/vllm/pull/47467
* @fenghourun made their first contribution in https://github.com/vllm-project/vllm/pull/47384
* @fjosw made their first contribution in https://github.com/vllm-project/vllm/pull/41026
* @guybd made their first contribution in https://github.com/vllm-project/vllm/pull/44029
* @harsha20032020 made their first contribution in https://github.com/vllm-project/vllm/pull/44720
* @hclsys made their first contribution in https://github.com/vllm-project/vllm/pull/44070
* @hhhhhhhhhhhhhhhhho made their first contribution in https://github.com/vllm-project/vllm/pull/46467
* @hillelda made their first contribution in https://github.com/vllm-project/vllm/pull/46069
* @I3eg1nner made their first contribution in https://github.com/vllm-project/vllm/pull/47532
* @imargulis made their first contribution in https://github.com/vllm-project/vllm/pull/46301
* @ItsMatti4 made their first contribution in https://github.com/vllm-project/vllm/pull/45263
* @jesco-absolut made their first contribution in https://github.com/vllm-project/vllm/pull/47589
* @jessiewei7 made their first contribution in https://github.com/vllm-project/vllm/pull/46560
* @jialoop-git made their first contribution in https://github.com/vllm-project/vllm/pull/45159
* @JohnLangford made their first contribution in https://github.com/vllm-project/vllm/pull/46835
* @Jyothirmaikottu made their first contribution in https://github.com/vllm-project/vllm/pull/47250
* @kalyanamdewri made their first contribution in https://github.com/vllm-project/vllm/pull/47517
* @Laurent-Zhang made their first contribution in https://github.com/vllm-project/vllm/pull/47429
* @lcheng321 made their first contribution in https://github.com/vllm-project/vllm/pull/45715
* @LiJzd made their first contribution in https://github.com/vllm-project/vllm/pull/45813
* @lslusarczyk made their first contribution in https://github.com/vllm-project/vllm/pull/43092
* @Lynn-hh made their first contribution in https://github.com/vllm-project/vllm/pull/46543
* @Meihan-chen made their first contribution in https://github.com/vllm-project/vllm/pull/44483
* @nagisa-kunhah made their first contribution in https://github.com/vllm-project/vllm/pull/44124
* @NathanielMcVicar made their first contribution in https://github.com/vllm-project/vllm/pull/45965
* @NicolasHug made their first contribution in https://github.com/vllm-project/vllm/pull/46609
* @omirosh made their first contribution in https://github.com/vllm-project/vllm/pull/44313
* @orestis-z made their first contribution in https://github.com/vllm-project/vllm/pull/46488
* @pranavthakur0-0 made their first contribution in https://github.com/vllm-project/vllm/pull/46306
* @Priyjain-amd made their first contribution in https://github.com/vllm-project/vllm/pull/45818
* @skajre made their first contribution in https://github.com/vllm-project/vllm/pull/46818
* @soaringk made their first contribution in https://github.com/vllm-project/vllm/pull/45810
* @spandantiwari made their first contribution in https://github.com/vllm-project/vllm/pull/46260
* @sriganesh123 made their first contribution in https://github.com/vllm-project/vllm/pull/35076
* @tarjan1 made their first contribution in https://github.com/vllm-project/vllm/pull/45657
* @thisjiang made their first contribution in https://github.com/vllm-project/vllm/pull/45924
* @umarkovi-amd made their first contribution in https://github.com/vllm-project/vllm/pull/46381
* @VectorPeak made their first contribution in https://github.com/vllm-project/vllm/pull/47099
* @wan-danfeng made their first contribution in https://github.com/vllm-project/vllm/pull/38174
* @yangyang-cs95 made their first contribution in https://github.com/vllm-project/vllm/pull/46684
* @yuyue0225sc made their first contribution in https://github.com/vllm-project/vllm/pull/44297
* @zhongjing123 made their first contribution in https://github.com/vllm-project/vllm/pull/47024
* @zhou9402 made their first contribution in https://github.com/vllm-project/vllm/pull/47448
* @ZichenYuan made their first contribution in https://github.com/vllm-project/vllm/pull/46452

## Contributors

Thank you to all the contributors who made this release possible!

@AndreasKaratzas, @njhill, @BugenZhao, @hmellor, @yewentao256, @WoosukKwon, @Sunt-ing, @micah-wil, @mgoin, @reidliu41, @peizhang56, @mawong-amd, @TheEpicDolphin, @jeejeelee, @taneem-ibrahim, @chaunceyjiang, @chaojun-zhang, @divakar-amd, @fxmarty-amd, @LopezCastroRoberto, @wzhao18, @mayuyuace, @jperezdealgaba, @noooop, @yzong-rh, @jikunshang, @zxd1997066, @bigPYJ1151, @yma11, @hickeyma, @benchislett, @xianbaoqian, @andakai, @NickLucche, @ivanium, @joerowell, @EazyReal, @mganczarenko, @majunze2001, @hongxiayang, @WindChimeRan, @Rohan138, @tjtanaa, @bbrowning, @thisjiang, @Fangzhou-Ai, @blasrodri, @Isotr0py, @zhenwei-intel, @zyongye, @frida-andersson, @muhammadfawaz1, @lcheng321, @spandantiwari, @Palaiologos1453, @soaringk, @Lynn-hh, @fadara01, @djramic, @Liangliang-Ma, @ronensc, @aarushjain29, @HDCharles, @qianlihuang, @AgenticSpark, @charlifu, @cleonard530, @shen-shanshan, @xaguilar-amd, @xiaohongchen1991, @varun-sundar-rabindranath, @gau-nernst, @tahsintunan, @GirasoleY, @hclsys, @Yejing-Lai, @LucasWilkinson, @matteso1, @akii96, @atalman, @lucianommartins, @I3eg1nner, @rahulssv-ibm, @ZichenYuan, @tanpinsiang, @hillelda, @Srinivasoo7, @Etelis, @Rukhaiya2004, @Oxygen56, @Priyjain-amd, @GuyStone, @nholmber, @CienetStingLin, @xinyu-intel, @JartX, @esmeetu, @hhhhhhhhhhhhhhhhho, @harsha20032020, @walterbm, @Acaciasama, @jessiewei7, @ashwin-phadke, @shivampr, @cyq1017, @kjiang249, @orestis-z, @xyang16, @tianmu-li, @mgehre-amd, @aaarkai, @guybd, @wcynb1023, @Josephasafg, @qyYue1389, @russellb, @haoyangli0109, @sfeng33, @mikekg, @EanWang211123, @ovidiusm, @ItsMatti4, @hyeongyun0916, @qli88, @juliendenize, @calvarado2004, @tdoublep, @brandonpelfrey, @davispuh, @weizhoublue, @jasonozuzu-cohere, @wentian-byte, @skajre, @gty111, @omirosh, @decarpentierg, @fjosw, @ilmarkov, @yuwenzho, @JisoLya, @JohnLangford, @aldenlobo, @bnellnm, @jasonlizhengjian, @zufangzhu, @izhuhaoran, @MatthewBonanni, @deng451e, @ashwing, @sriganesh123, @linitra24, @liranschour, @umarkovi-amd, @aman0603, @adobrzyn, @jwzheng96, @eicherseiji, @ArsalanShakil, @tc-mb, @imargulis, @fangyuchu, @puririshi98, @JeanPaulShapo, @VectorPeak, @tarjan1, @qiching, @Achyuthan-S, @ZJY0516, @lucifer1004, @cinnamonica02, @jmamou, @almayne, @hao-aaron, @Jyothirmaikottu, @andylolu2, @AIvashov, @stevenkuang-tencent, @lcskrishna, @Aneureka, @wan-danfeng, @chengzheng345, @pranavthakur0-0, @zRzRzRzRzRzRzR, @DanBlanaru, @adamkbaranowski, @wendyliu235, @eparshut, @yangyang-cs95, @kalyanamdewri, @maxdebayser, @fenghourun, @tpopp, @okorzh-amd, @labAxiaoming, @sychen52, @ekagra-ranjan, @gausah01, @yuyue0225sc, @cpersson-amd, @lslusarczyk, @alex101-ops, @Zhenzhong1, @velonica0, @zhongjing123, @zhou9402, @llsj14, @majian4work, @akinsella, @BadrBasowid, @afierka-intel, @ayush1399, @LiJzd, @jesco-absolut, @Laurent-Zhang, @Kevin-XiongC, @NathanielMcVicar, @askliar, @ACEEE-1222, @jinzhen-lin, @SherryC41, @simondanielsson, @nv-nedelman-1, @yisustc, @kylesayrs, @jialoop-git, @NicolasHug, @guan404ming, @HumphreySun98, @danielafrimi, @gcanlin, @robertgshaw2-redhat
