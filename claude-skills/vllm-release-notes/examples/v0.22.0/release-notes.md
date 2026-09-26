## Highlights

This release features 459 commits from 230 contributors (63 new)!

* **DeepSeek V4 maturity**: DeepSeek V4 received a major hardening pass this cycle — the model was reorganized into a dedicated `vllm/models/deepseek_v4/` package (#43004, #43039, #43073, #43077, #43149), gained NVFP4 fused MoE support (#42209), full + piecewise CUDA graph (#42604), and MTP speculative decoding (#43385). A large set of fused kernels (MegaMoE, `mhc`, Q-norm, indexer, sparse MLA) and ROCm parity fixes landed alongside accuracy fixes (#42810, #43710).
* **Model Runner V2 advances toward default**: MRv2 added an oracle that selects MRv2 for Qwen3 dense models by default (#39337), sleep-mode weight reload (#42673), `update_config` (#42783), and shared KV-cache layers (#35045), plus many correctness fixes. It now falls back to MRv1 automatically when a KV connector is present (#42955).
* **Experimental Rust frontend**: A new Rust front-end integration landed (#40848), with the implementation moved into the tree (#43283) and a DP Supervisor for data-parallel serving (#40841).
* **Batch invariance, faster**: Batch-invariant inference gained Cutlass FP8 support for a **28.9% end-to-end latency improvement** (#40408), compile-mode support on SM80 (#42456), and an NVFP4 Cutlass linear path (#39912).
* **Multi-tier KV cache offloading**: A new multi-tier KV cache offloading framework (#40020) with a Python filesystem secondary tier (#41735), DSv4 support (#43142), and Mooncake disk offloading (#42689) extends offloading beyond CPU memory.

### Model Support
* New architectures: MiniCPM-V 4.6 (#41254), InternS2 Preview (#42705), OpenVLA (#42654), MolmoWeb `hf_overrides` docs (#42163); EXAONE-4.5 aligned with Transformers update (#42246).
* Speculative decoding: custom callable proposer backend (#39487), post-norm EAGLE-3 speculators (#42764), peagle speculators (#41826), hybrid-attention models in `extract_hidden_states` (#39949), non-MTP speculation for NemotronH (#43130), shared MTP weights in MRv2 (#42538).
* DeepSeek V4: NVFP4 MoE (#42209), CUDA graph full/piecewise (#42604), MTP (#43385), model package refactor (#43004, #43039, #43073, #43077), sparse MLA + compressor refactor (#43149, #43710), MegaMoE input-prep kernel move (#43632).
* Qwen3.5/3.6: GDN output-projection flatten (#42311), GatedDeltaNet Marlin TP≥2 fix (#36329), ViT full CUDA graph (#42151), runai-streamer weight loading for Qwen3.5/MTP/Qwen3-VL (#42521, #42716), KDA chunk-prefill exp2 semantics (#43195).
* Gemma3/Gemma4: mixed-resolution image co-batching crash fix (#42217), MoE routing closure fix (#42250), tool-parser float-corruption fix (#42128), batched vision encoder for image/video (#43169), multi-GPU fix (#42630).
* Kimi-K2.5: skip vision-tower dtype conversion under quantization (#42869), `mm_projector` dtype fix (#42081).
* Cohere: enable Cohere MoE (#43143), pipeline parallelism for Cohere vision (#42819).
* Tool calling: Apertus tool parser (#41154), Qwen3Coder `anyOf`/`oneOf`/`$ref` resolution re-land (#37831), shared `coerce_to_schema_type` across MiniMax-M2 / DeepSeek-V3.2 / Seed-OSS parsers (#43006, #43019, #43140).
* ViT CUDA graph: Qwen2-VL (#41736), Step3-VL encoder (#42224), Qwen3.5 (#42151), FlashInfer metadata for Qwen2.5-VL vision attention (#42787).

### Engine Core
* Model Runner V2: Qwen3-dense-by-default oracle (#39337), sleep-mode reload weights (#42673), `update_config` (#42783), shared KV-cache layers (#35045), FP32 gumbel sampling (#41775), auto-fallback to MRv1 with connectors (#42955), `logprob_token_ids` correctness (#43125, #41761), prompt-logprobs size fix (#42778).
* KV offloading: multi-tier framework (#40020), Python filesystem secondary tier (#41735), DSv4 support (#43142), tier-offload follow-up (#42529), prefer HND layout (#41928), `reset_cache()` (#41956), per-request tracking (#42507), store-deferral fix (#41945).
* MoE refactor: `ExpertMapManager` (#41046), experts moved to `experts/` (#42334), `RoutedExperts` alias for FusedMoE (#40735), EPLB refactoring for FusedMoE (#41055).
* Mamba: attention module refactor (#41126), Mamba2 SSD kernel warmup (#39822), bf16 SSM cache (#41680), GPU-side state postprocessing fused kernel (#40172), run single-token extends as decodes (#42430).
* KV events: emit KV cache metadata (#40984).
* Allocator: manual cumem allocator enable (#33648), stream-aware free callback (#43020).
* elastic-EP: stage/commit MoE quant method on reconfigure (#40881).

### Hardware & Performance
* **NVIDIA Blackwell / SM12x**: FlashInfer b12x MoE + FP4 GEMM for SM120/121 (#40082), per-tensor FP8 CUTLASS on SM12.1 (#41215), `head_dim=512` for FlashInfer TRTLLM attention (#38822), FlashInfer Blackwell GDN prefill (#40717), GDN prefill kernel for SM100 (#43273).
* **Performance**: batch-invariant Cutlass FP8 (+28.9% E2E) (#40408), CutlassFP8 padding pre-processing (+13.5% TTFT) (#42651), padded NVFP4 quant kernel (+2.4–5.7% E2E) (#42774), GPU<->CPU sync elimination 1/n (#41429) and 4/n (#42347), fused RoPE+KVCache+q_concat for MLA (#40392), MLA `compute_prefill_context` / `_v_up_proj` optimizations (#42460, #42561), penalties Triton kernel (#40657), `do_not_specialize` in fused FP8 RoPE (#42849), FULL CUDA graph capture for TRITON_MLA decode (#42885).
* **AMD ROCm**: DSV4 functionality + accuracy fixes (#42810, #43679 Tilelang MHC), flash sparse MLA Triton kernels (#41812), gluon paged MQA logits on gfx950/MI355X (#42062), RMSNorm+Quant fusion for gfx950 (#41825), AITER FA backend cleanup (#41942), XGMI backend for MoRI connector (#41753), QuickReduce min-size override (#41675), DSV4 MTP (#43385).
* **CPU / RISC-V**: RVV-optimized attention kernels for RISC-V Vector Extension (#40119) with VLEN=256 (#42943), fused GDN for AMX CPU (#42707), MXFP4 W4A16 MoE (#41922), experimental Triton + MRv2 on CPU (#43225), improved CPU thread utilization (#42666), `--cpu-distributed-timeout-seconds` (#42968).
* **Intel XPU**: GPTQ int4 support (#37844), mxfp8 MoE (#41918), FP8 block-scaled quantization (#42952), custom-op collective behavior (#41354), multiple sparse-attention kernels (#37888), MoE topk routing + MXFP4 fallback (#42951), CT W4A4 MXFP4 path (#38896), reduced XPU MoE host overhead (#42915).
* **Kernel ABI**: continued migration to libtorch stable ABI — 5/n (#42339), 6/n (#42663), 7/n (#43209).
* **Experimental**: breakable CUDA graph (#42304).

### Large Scale Serving
* Disaggregated serving (NIXL): lease-renewal TTL for KV blocks on P (#41383), handshake-failure policy honoring (#40364), GDN support for PD with NIXL (#41869), multi-node TP>8 fix (#39907), side-channel host-selection fix (#41806).
* Mooncake: disk offloading in MooncakeStoreConnector (#42689), HMA support for DSV4 (#42828), operation metrics (#43392), load-failure propagation (#42788), block-aligned full hits (#43494), finish-after-preemption handling (#43281).
* Data parallel: DP Supervisor (#40841), publish request counts at engine-step start (#41626), forward `X-data-parallel-rank` header (#42330).
* EPLB: change default EPLB communicator (#43110), VLM-wrapper init fix (#39805), remove dead `torch.accelerator.synchronize()` (#40733).
* LoRA: one-shot Triton kernel for MoE LoRA (#42290), simultaneous 2D & 3D MoE LoRA adapters (#42242), reduced 2D-weight memory under EP (#42737), MoE LoRA align-kernel grid fix (#40131).

### Quantization
* **MXFP4**: linear layers + compressed-tensors integration (#41664), CPU W4A16 MoE (#41922), XPU mxfp8 MoE (#41918).
* **NVFP4**: DeepSeek V4 fused MoE (#42209), ModelOpt W4A16 NVFP4 fused MoE + mixed-precision dispatch (#42566), batch-invariant NVFP4 Cutlass linear (#39912), FlashInfer TRTLLM NvFP4 monolithic MoE routing fix (#43223), TRTLLM NVFP4 MoE chunking fix (#43599).
* **Quark**: load Quark NVFP4 checkpoints (#35859), W8A8 INT8 garbage-output fix on Step-3.5-Flash (#41892), W4A4 oracle refactor (#41436).
* **AutoRound**: W4A16 support (#39778).
* **ModelOpt**: Qwen3.5/3.6 VLM quantized prefix mapping (#42546).
* **Framework**: rework `quantization_config` to use `QuantKey` with activation override (#41566), MoE W4A8 CT migrated to oracle (#42680), AWQ Marlin MoE onto modular WNA16 oracle (#42483), GPTQ consolidation (`gptq_marlin` → `auto_gptq`) (#38288).

### API & Frontend
* **Rust frontend**: integration (#40848), in-tree code move (#43283), utility call-ID newtype (#43405), simplified `AuthenticationMiddleware` path extraction (#43426).
* **Responses API**: `chat_template_kwargs` support (#42272), message-merging fix (#42189), empty channel/recipient harmony fix (#35540).
* **Completions**: `thinking_token_budget` support (#42116) with inverted-condition fix (#41674); map `reasoning_effort` to `enable_thinking` (#43401).
* **Frontend**: truncation side for OpenAI endpoints (#43260), normalize `reasoning_content` → `reasoning` (#42664), reworked fastokens integration (#43168), consolidated Speech-to-Text entrypoints (#42370, #42274), beam-search consolidation via `BeamSearchMixin` (#42946), score/rerank chat-template instructions (#42412).
* **Auth**: API-key authorization for `/v2` endpoints (#42594).
* **Offline API**: pooling offline API split into `PoolingOfflineMixin` (#42267), split offline inference APIs/utils (#43553).

### Build & Dependencies
* CUDA 12.9 wheel builds switched to PyTorch `manylinux_2_28` base (#41668).
* FlashInfer bumped to v0.6.11.post2 (#41711); `nvidia-cutlass-dsl` to 4.5.2 (#42991, #43230, #43745); llguidance to 1.7 (#42150); `triton_kernels` downgraded to v3.5.1 for gpt-oss (#43135).
* Rust frontend build: `setuptools-rust` dependency (#43287, #43377), pinned `protoc` in rust-build stages (#43292).
* Docker: non-root `vllm-openai` target (#40275), build `mooncake-transfer-engine` from source (#42114), AINIC & Thor NIC support (#40453); Python-only installation made optional (#42293).
* vllm-tpu: disable build isolation for CUDA deps (#43038), tpu-inference docker build fix (#43360).
* `humming` MoE backend dependency added, reverted, then restored with CuPy runtime fix (#42540, #43492, #43530).

### Deprecations & Removals
* Removed old locations of `get_tokenizer` and `resolve_hf_chat_template` (#35024).
* Marked env vars now covered by `--moe-backend` / `--linear-backend` (#43148).
* Removed deprecated MLA prefill arguments (#42555).
* Removed dead CUDA kernels and dead code (#42767, #42889, #43144).

## Contributors

@yewentao256, @haosdent, @njhill, @mgoin, @jeejeelee, @AndreasKaratzas, @NickLucche, @sfeng33, @noooop, @WoosukKwon, @khluu, @taneem-ibrahim, @Dao007forever, @vadiklyutiy, @bnellnm, @ivanium, @tjtanaa, @mmangkad, @hmellor, @DarkLight1337, @hickeyma, @zhenwei-intel, @jikunshang, @ronensc, @benchislett, @hao-aaron, @arpera, @zyongye, @gau-nernst, @frida-andersson, @ZhanqiuHu, @cleonard530, @akii96, @bedeks, @Isotr0py, @JasonKeyiL, @bigPYJ1151, @zhewenl, @weizhoublue, @zxd1997066, @gnovack, @chaojun-zhang, @majian4work, @chaunceyjiang, @pschlan-amd, @amitz-nv, @yma11, @dsikka, @tc-mb, @shanjiaz, @jperezdealgaba, @yzong-rh, @viktorpusTT, @TheEpicDolphin, @MatthewBonanni, @shen-shanshan, @hallerite, @zufangzhu, @bbrowning, @divakar-amd, @ianliuy, @esmeetu, @rasmith, @louie-tsai, @pmaybank, @liulanze, @ZJY0516, @TheDuyIT, @wzhao18, @jinzhen-lin, @BugenZhao, @ashwing, @fuergaosi233, @hqhq1025, @shaharmor98, @pisceskkk, @lkm2835, @noa-neria, @Rohan138, @whx-sjtu, @vrdn-23, @alexagriffith, @Flink-ddd, @jeffreywang-anyscale, @skyloevil, @ymoslem, @Lucaskabela, @kg6-sleipnir, @woernfl, @tdoublep, @GOavi101, @jmamou, @PeaBrane, @KaivalyaMDabhadkar, @BWAAEEEK, @MrZ20, @afierka-intel, @JoursBleu, @hissu-hyvarinen, @mwawrzos, @CynicDora, @NoeliaBentancor, @johncalesp, @fynnsu, @fxmarty-amd, @walterbm, @liangel-02, @lgeiger, @he-yufeng, @abinggo, @KrxGu, @hks-9697-v2, @Sarah-Salah, @rebklee, @aoshen02, @haic0, @libinta, @Zhenzhong1, @xhx1022, @b-mu, @WindChimeRan, @tpopp, @charlifu, @chengyinie, @ricky-chaoju, @lyd1992, @daniel-devlab, @paulyu12, @bobofang11235, @laudney, @BadrBasowid, @maeehart, @PatchouliTIS, @chunxiaozheng, @blake-snc, @southfreebird, @rbrugaro-amd, @rasdani, @dusthunter, @qizzzh, @ProExpertProg, @qianlihuang, @alec-flowers, @JisoLya, @gaozihao-shy, @rishaps, @xyang16, @wendyliu235, @hlin99, @tianmu-li, @yuwenzho, @inisis, @kfirtoledo, @roikoren755, @liranschour, @vllm-agent, @blancsw, @netanel-haber, @BowenBao, @czhu-cohere, @amitport, @tuukkjs, @revit13, @ofirzaf, @qyYue1389, @junyanxu, @gracie-guo, @sagearc, @xinyu-intel, @yiwen101, @DomBrown, @tomeras91, @Dogacel, @maxdebayser, @fadara01, @Terrencezzj, @izikgo, @wangrui6, @kebe7jun, @rishitdholakia13, @j9smith, @meena-at-work, @dllehr-amd, @alexeldeib, @sonusflow, @lucianommartins, @AAISSJ, @DaoyuanLi2816, @zexplorerhj, @zhangxin81, @velonica0, @fuscof-ibm, @anishesg, @zhengluo-nv, @ylangtsou, @fangyuchu, @zx3xyy, @simondanielsson, @ruizhang99, @zixi-qi, @xwu-intel, @yufufi, @wdhongtw, @mrjunwan-lang, @wangxiyuan, @wasnertobias, @ilmarkov, @sychen52, @zhandaz, @russellb, @SandishKumarHN, @juhi10071998, @itayalroy, @djmmoss, @SumanthRH, @mayuyuace, @zhougit86, @meenchen, @lucifer1004, @popkart-EZ, @jzakrzew, @ffggs, @huanghua1994, @orozery, @danisereb, @rshavitt, @Yihuki, @QingZhou-YangHY, @Jie-Fang, @bbartels

## New Contributors

* @abinggo made their first contribution in https://github.com/vllm-project/vllm/pull/42128
* @afierka-intel made their first contribution in https://github.com/vllm-project/vllm/pull/40327
* @alexagriffith made their first contribution in https://github.com/vllm-project/vllm/pull/41987
* @alexeldeib made their first contribution in https://github.com/vllm-project/vllm/pull/43255
* @amitport made their first contribution in https://github.com/vllm-project/vllm/pull/41666
* @anishesg made their first contribution in https://github.com/vllm-project/vllm/pull/43079
* @bedeks made their first contribution in https://github.com/vllm-project/vllm/pull/40269
* @blake-snc made their first contribution in https://github.com/vllm-project/vllm/pull/35568
* @blancsw made their first contribution in https://github.com/vllm-project/vllm/pull/41154
* @bobofang11235 made their first contribution in https://github.com/vllm-project/vllm/pull/42604
* @BWAAEEEK made their first contribution in https://github.com/vllm-project/vllm/pull/42233
* @CynicDora made their first contribution in https://github.com/vllm-project/vllm/pull/39487
* @daniel-devlab made their first contribution in https://github.com/vllm-project/vllm/pull/42479
* @DaoyuanLi2816 made their first contribution in https://github.com/vllm-project/vllm/pull/42905
* @Dogacel made their first contribution in https://github.com/vllm-project/vllm/pull/42764
* @DomBrown made their first contribution in https://github.com/vllm-project/vllm/pull/42080
* @dusthunter made their first contribution in https://github.com/vllm-project/vllm/pull/42594
* @ffggs made their first contribution in https://github.com/vllm-project/vllm/pull/43414
* @frida-andersson made their first contribution in https://github.com/vllm-project/vllm/pull/41825
* @fuergaosi233 made their first contribution in https://github.com/vllm-project/vllm/pull/43488
* @gaozihao-shy made their first contribution in https://github.com/vllm-project/vllm/pull/42869
* @gracie-guo made their first contribution in https://github.com/vllm-project/vllm/pull/42626
* @haic0 made their first contribution in https://github.com/vllm-project/vllm/pull/40453
* @hks-9697-v2 made their first contribution in https://github.com/vllm-project/vllm/pull/42521
* @hlin99 made their first contribution in https://github.com/vllm-project/vllm/pull/42740
* @inisis made their first contribution in https://github.com/vllm-project/vllm/pull/41710
* @izikgo made their first contribution in https://github.com/vllm-project/vllm/pull/42938
* @j9smith made their first contribution in https://github.com/vllm-project/vllm/pull/41215
* @junyanxu made their first contribution in https://github.com/vllm-project/vllm/pull/42671
* @KaivalyaMDabhadkar made their first contribution in https://github.com/vllm-project/vllm/pull/42333
* @libinta made their first contribution in https://github.com/vllm-project/vllm/pull/41689
* @lucifer1004 made their first contribution in https://github.com/vllm-project/vllm/pull/43433
* @meena-at-work made their first contribution in https://github.com/vllm-project/vllm/pull/40082
* @mrjunwan-lang made their first contribution in https://github.com/vllm-project/vllm/pull/43360
* @MrZ20 made their first contribution in https://github.com/vllm-project/vllm/pull/42394
* @mwawrzos made their first contribution in https://github.com/vllm-project/vllm/pull/42498
* @NoeliaBentancor made their first contribution in https://github.com/vllm-project/vllm/pull/42250
* @ovidiusm made their first contribution in https://github.com/vllm-project/vllm/pull/42542
* @paulyu12 made their first contribution in https://github.com/vllm-project/vllm/pull/42306
* @QingZhou-YangHY made their first contribution in https://github.com/vllm-project/vllm/pull/43579
* @qizzzh made their first contribution in https://github.com/vllm-project/vllm/pull/41680
* @qyYue1389 made their first contribution in https://github.com/vllm-project/vllm/pull/42289
* @rasdani made their first contribution in https://github.com/vllm-project/vllm/pull/42481
* @rebklee made their first contribution in https://github.com/vllm-project/vllm/pull/42098
* @revit13 made their first contribution in https://github.com/vllm-project/vllm/pull/42926
* @ruizhang99 made their first contribution in https://github.com/vllm-project/vllm/pull/43260
* @Sarah-Salah made their first contribution in https://github.com/vllm-project/vllm/pull/42441
* @sonusflow made their first contribution in https://github.com/vllm-project/vllm/pull/36329
* @TheDuyIT made their first contribution in https://github.com/vllm-project/vllm/pull/40131
* @tuukkjs made their first contribution in https://github.com/vllm-project/vllm/pull/42880
* @vllm-agent made their first contribution in https://github.com/vllm-project/vllm/pull/42913
* @wangrui6 made their first contribution in https://github.com/vllm-project/vllm/pull/40326
* @wasnertobias made their first contribution in https://github.com/vllm-project/vllm/pull/43001
* @weizhoublue made their first contribution in https://github.com/vllm-project/vllm/pull/42830
* @woernfl made their first contribution in https://github.com/vllm-project/vllm/pull/42397
* @xwu-intel made their first contribution in https://github.com/vllm-project/vllm/pull/37888
* @Yihuki made their first contribution in https://github.com/vllm-project/vllm/pull/42933
* @yiwen101 made their first contribution in https://github.com/vllm-project/vllm/pull/42654
* @ylangtsou made their first contribution in https://github.com/vllm-project/vllm/pull/43038
* @yufufi made their first contribution in https://github.com/vllm-project/vllm/pull/42972
* @zhengluo-nv made their first contribution in https://github.com/vllm-project/vllm/pull/43105
* @zhougit86 made their first contribution in https://github.com/vllm-project/vllm/pull/42739
* @zx3xyy made their first contribution in https://github.com/vllm-project/vllm/pull/42855
