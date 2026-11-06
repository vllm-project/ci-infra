# v0.28.0

## Highlights

This release features 584 commits from 270 contributors (76 new)!

* **Kimi-K3 performance push**: a major optimization effort for Kimi-K3 across the stack — Decode Context Parallel (DCP) support (#50484), fused FlashKDA decode and prefill kernels (#50654, #51311, #52458), SiTU activation support for MegaMoE (#50510), GEMM-RS for sequence parallelism (#52079), combined all-gathers with 1.5~3x kernel-level speedup (#51070), an adaptive speculative token budget delivering ~60% better DSpark TTFT (#51725), and optional shared-expert sharding saving ~17 GiB of memory per GPU (#50912). Kimi-K3 also now runs on ROCm with the V2 model runner (#51653).
* **DeepSeek V4 end-to-end**: sparse MLA now works end-to-end for plain decode, MTP, and DSpark speculative decoding (#51538), joined by AMD Quark NVFP4 support (#47972), reasoning-effort prompts and mappings (#50580), sparse top-k metadata kernel optimizations (#52084, #51967), narrowed eager CUDA graph regions (#51430, #52401), and ROCm enablement on gfx11 and gfx950 (#47017, #52212).
* **Speculative decoding advances**: DFlash2 with local convolution and a candidate selector (#52816), DSpark confidence-scheduled verification (#47808), and async scheduling auto-enabled for draft models (#48341).
* **Model Runner V2 maturation**: E/P/D disaggregation (#38390), weight offloading (#51413), multi-layer MTP KV cache support (#50062), encoder CUDA graphs (#49852), decoder token-wise pooling (#50931) plus Transformers pooling models (#52425), attention-free models (#52374), and `thinking_token_budget` support (#46727).
* **Tiered KV cache offloading**: disk offloading support (#49644), out-of-tree secondary tier managers via `module_path` (#51007), partial secondary-tier load results (#50321), tiering metrics (#48798), and a canonical CPU layout for parallelism-agnostic offload (#48414).
* **Rust frontend & gRPC**: a standalone renderer (#50289), multimodal image inference over gRPC (#50368), explicit data-parallel rank routing (#51178), and RL lifecycle control (#51316), with protobuf schemas now published to Buf (#51276).
* **New defaults**: `max_num_batched_tokens` raised from 8192 to 16384 (#51726), prefix caching enabled by default for Mamba models (#50991), and the Blackwell CUDA graph capture default raised to 1024 (#49390).
* **Breaking changes**: bitsandbytes support migrated to an out-of-tree plugin (#43529); Transformers bumped to 5.15.0 (#51668); the deprecated `calculate_kv_scales` runtime KV scale calculation was removed (#49389); `override_attention_dtype` was removed (#48684).

## Model Support
* **New models**: Muse Glimmer (#51655), Ling 3.0 Flash with BF16, MTP, and parser support (#51045) plus an FP8 variant (#51265) and hybrid MXFP4 routed experts (#52114), Dots3 NOTE native multimodal support (#51255), and Interns2mobius (#51149).
* **Qwen**: Qwen3.8 enabled on AMD ROCm (#50068), fused CUDA post-conv MTP decode kernel for Qwen3.5 GDN (#51674), GDN gates aligned with speculative tokens (#51812), and Qwen3.5 fixes for text-only checkpoints (#50734, #50355).
* **Transformers modeling backend**: MLA support (#48250), hardware-agnostic model definition (#49458), fully generalized input embedding handling (#51247), logit softcapping (#52173), and a hardened multimodal path (#51408, #51657).
* **LoRA**: vision tower LoRA for Gemma4 (#42662), tower/connector LoRA for Keye (#51780) and Ultravox (#48215).
* **Vision encoders**: ViT full CUDA graph for Kimi-K2.5 (#50929) and Ernie-4.5-VL (#45254, #51461), torch.compile for the Qwen3-VL encoder (#40116), and long-blocking H2D copies avoided in ViT (#51841).
* **MoE**: extended EPLB support for Mistral Large 3 and additional MoE backends (#48355), CuTe DSL skinny GEMM extended to GLM-5.2 (#49791).
* **Speculative decoding coverage**: EAGLE3 support declared on KimiLinear (#52171), Qwen3.6 dSpark acceptance coverage (#51310).
* **Correctness**: MiniMax-M3 NVFP4 inference (#48929) and compressed-tensors FP8 MoE SwiGLU params (#46845), Gemma3n/Gemma4 variable-length audio batch padding (#50958), Gemma 4 compatibility with the upcoming Transformers version (#49797), and a Qwen3-Omni crash on video without an audio track (#48420).
* **Multimodal performance**: fused on-device multimodal preprocess normalization (#50411), faster placeholder and token-match scanning (#50716), and repeated prompt-update scans avoided (#51774).

## Engine Core
* **Speculative decoding**: DSpark confidence-scheduled verification (#47808), top-k DSpark Markov projection (#49969), DFlash2 with local convolution and a candidate selector (#52816), async scheduling auto-enabled for draft models (#48341), fused MTP trailing all-reduce with local-argmax draft tokens (#49793), and an adaptive budget for speculative scheduled input tokens (#51725).
* **KV cache & scheduling**: per-request scheduling for MLA chunked context (#50613), partial-tail prefix reuse with fine-grained prefix matching (#50507), backend-published KV packing in the KV-cache layout refactor (#51612, #51704), LIFO free-block reuse order restored when prefix caching is off (#51482), and silent request skipping in priority scheduling fixed (#49206).
* **Performance**: continued elimination of GPU<->CPU syncs on the execution path (#51458, #51738, #52369) now guarded by a CI sync check (#43107), new JIT warmup infrastructure with predicate filtering (#49315), the top-k/top-p Triton sampler launched with 8 warps (#51507), detokenization skipped in offline beam search (#50333), Mask Replay (#49577), optimized long-context MLA cache gathers (#51739), and HF revisions resolved to a commit hash once per model load (#49990).
* **Hybrid/Mamba**: prefix caching on by default (#50991), the final part of the Mamba attention module refactor (#44857), 3D-grid tiling of the state-copy Triton kernels (#49436), and Mamba alignment applied before encoder caps (#51603).
* **RL workflows**: stateful trainer send over NCCL and sparse NCCL (#50902), `CuMemAllocator.discard()` for tag-selective GPU memory release (#52514), level-2 sleep/wake/reload fixed with LoRA enabled (#39935), and rewritten weight-transfer docs with standardized examples (#51729).
* **Startup robustness**: file:// rendezvous for single-node executors eliminates startup port races (#50999, #51652), frontend processes are watched during engine startup (#43417), a `get_open_port()` livelock on DP-reserved ports was fixed (#50965), and NVML is no longer re-initialized on every device-capability check (#50393).

## Hardware & Performance
* **NVIDIA**: FlashInfer XQA decode support on SM12x (#49718), a CuTeDSL fused query kernel on SM100 (#49792), programmatic dependent launch for the DSA decode kernels (#50230), the native DSA decode path for MTP=3 on SM90 (#52164), GB10 fused-MoE FP8 tuning configs (#52502), and B12X dense linear backends (#52016).
* **AMD ROCm**: torch 2.12 / triton 3.7 stack bump (#50607), AITER and FP8 inference enabled on GFX120x (#43615), DeepSeek-V4 on gfx11 (#47017), optimized Triton sparse-MLA decode on gfx950 (#52212), FlyDSL decode-attention kernel for 4-bit TurboQuant KV cache (#47896) and an fp8 MQA logits kernel on gfx942 (#49544), a fused Kimi-K3 KDA decode kernel (#50654), fused bf16→fp32 router GEMM (#50268), pinned memory on supported WSL2 kernels (#50126), and preshuffled sparse indexing for 16-token blocks (#51216).
* **Intel XPU**: a torch linear backend including blockwise GEMM (#49664, #50826), MXFP8 linear weights for the INC DeepSeek V4 model (#48476), async-scheduling PP sampled-token broadcast overlapped with compute (#51650), an XPU wheel added to the release pipeline (#52108), tuned Mamba SSU configs for Arc Pro B70 (#50534), and UVA weight offloading fixes (#51770).
* **CPU**: an MLA backend so DeepSeek-V2/V3 can run on CPU (#49453), a triton-cpu wheel (#52092), GPTQ and AWQ enabled on s390x (#51148) along with tcmalloc (#50841), BF16 MoE routed through zentorch on AMD (#44201), an unquantized MoE backend for Power (VSX) (#51624), unquantized MoE migrated to the modular-kernel experts structure (#50133), and the MXFP4 block scale folded in 2 instructions instead of 4 (#51583).

## Large Scale Serving
* **E/P/D disaggregation**: Model Runner V2 E/P/D support (#38390), duplicate image preprocessing removed with GPU-side preprocessing (#50390), KV consumers may omit multimodal embeddings (#52697), encoder-instance requests kept alive until their images are encoded (#50275), and EC connector scheduler/worker metadata plumbing (#49579, #49585).
* **KV offloading**: disk offloading for SimpleCPUOffloadConnector (#49644), out-of-tree secondary tier managers via `module_path` (#51007), partial secondary-tier load results (#50321), tiering offloading metrics (#48798), data-parallel topology exposed to offloading backends (#51879), a canonical CPU layout for parallelism-agnostic offload (#48414), and quadratic ARC batch eviction avoided (#50992).
* **Mooncake**: store group semantics (#44956), tenant ID support (#48069), and official wheels in the Docker image (#51067).
* **Connectors**: transfer mode (push/pull) included in the NIXL compatibility hash (#50620), a MoRIIO per-layer READ-completion barrier (#48534), 2P2D wide-EP with mori-ep/mori-io at dp=ep=16 (#45043), and stale remote cleanup in the push connector (#50234).
* **Parallelism**: EPLB balancedness calculation fixed and tested (#51813), dense multinode DP rescope (#49212).

## Quantization
* **Online quantization**: online MXFP4 support (#49347), online weight scales shared across TP (#49764), precision preserved in online NVFP4 expert packing (#50029), and the online NVFP4 MoE kernel reused across reloads (#50074).
* **NVFP4**: batch-invariant NVFP4 MoE via CUTLASS (#40372), KV 4-over-6 scale search (#45187), CuTeDSL MoE with SwiGLU-OAI and ReLU2 activations (#47106), and out_dtype matched to the model dtype (#48861).
* **New kernels**: block-wise scaled_mm (#49932), DeepSeek-V4 AMD Quark NVFP4 with an emulation kernel (#47972).
* **Fixes**: dynamic INT8 W8A8 MoE config no longer built as W8A16 (#50833) and a TritonExperts crash (#51411), MXFP4 conversion for FlashInfer CUTLASS (#51038), fp32 weight scales and per-expert checkpoint mapping for MXFP4 (#51419), and fused block-scale orientation (#50727).

## API & Frontend
* **New capabilities**: request priority parsed from an HTTP header (#51089), session ID plumbing into requests (#48048), `count_reasoning_tokens` in the streaming parser engine (#45802), `content_parts` on `/inference/v1/generate` (#51478), `model` optional on all `/derender` request classes (#51463), output token IDs logged at DEBUG level (#52098), and vLLM Recipes connected to native config-based deployment and benchmarking (#51308, #51878).
* **Rust frontend**: a standalone renderer (#50289), gRPC multimodal image inference (#50368), explicit data-parallel rank routing (#51178), RL lifecycle control (#51316), dynamic tools from developer messages (#51144), protobuf schemas published to Buf (#51276), and MiniJinja upgraded to 2.22 (#51235).
* **Anthropic API**: 4xx returned for client-caused errors on `/v1/messages` (#52246), `disable_parallel_tool_use` preserved (#52021), and stop sequences bounded (#51997).
* **Cohere**: upstreamed parser fixes (#51998), stop sequences reported correctly (#51556), and vectorized binary embedding bit-packing (#52277).
* **Structured output**: request stop tokens masked in xgrammar until the grammar terminates (#49227, #50595), NUL bytes rejected in `structured_outputs.regex` (#51796), negative token IDs rejected as out-of-vocabulary (#51795), and `VLLMValidationError` raised from validators (#52394).
* **Robustness**: uvicorn signal handlers disabled instead of racing them (#50916), a consolidated entrypoint exception handler (#52261), 400 instead of 500 on non-object JSON bodies (#51654, #52528), generation inputs bounded before expensive work (#51447), and `cache_salt` now required to be non-empty (#50816).

## Security
* Fixed a DoS via sample-rate forgery that bypassed the audio decode duration guard (#49948); the audio decode duration limit is now also enforced in NanoNemotronVL (#50221).
* DeepStream classified as a GPU backend with pixel limits enforced (#50755).
* `_load_ov2_processor` guarded with `resolve_trust_remote_code` (#52952).
* Documentation now warns that `--api-key` does not gate all endpoints (#51999).

## Dependencies
* Transformers 5.15.0 (#51668), huggingface-hub 1.27.0 (#51422), fastsafetensors upgrade (#50827).
* ROCm: torch 2.12, triton 3.7, torchaudio, torchvision (#50607).
* Runtime image upgraded to Ubuntu 24.04, picking up rdma-core > 44 (#51058).
* DeepGEMM pinned to the deepseek-ai nv_dev tip (#52035), DeepEP pinned by full commit hash (#52028), FlashAttention 3 built with the torch stable API (#49599).

## Breaking Changes & Deprecations
* bitsandbytes support is now an out-of-tree plugin (#43529).
* The deprecated `calculate_kv_scales` runtime KV scale calculation was removed (#49389).
* `override_attention_dtype` was removed (#48684).
* `reasoning_content` output removal is documented as a breaking client change (#50624).
* KV offload tiering metrics renamed from `kv_offload_tiering_block_{queries,hits}` to `..._chunk_...` (#52812).
* MoE legacy code removed (#51078).

## New Contributors
* @acheamponge made their first contribution in https://github.com/vllm-project/vllm/pull/49353
* @acmore made their first contribution in https://github.com/vllm-project/vllm/pull/51259
* @anhtra3889 made their first contribution in https://github.com/vllm-project/vllm/pull/51002
* @anujbolewar made their first contribution in https://github.com/vllm-project/vllm/pull/50867
* @arthurgao2003 made their first contribution in https://github.com/vllm-project/vllm/pull/48215
* @baodii made their first contribution in https://github.com/vllm-project/vllm/pull/51215
* @bitborne made their first contribution in https://github.com/vllm-project/vllm/pull/44956
* @bohnstingl made their first contribution in https://github.com/vllm-project/vllm/pull/49458
* @ccaadaro made their first contribution in https://github.com/vllm-project/vllm/pull/51583
* @cmiyai made their first contribution in https://github.com/vllm-project/vllm/pull/46870
* @d4l3k made their first contribution in https://github.com/vllm-project/vllm/pull/51097
* @dmai-afk made their first contribution in https://github.com/vllm-project/vllm/pull/51120
* @dmholtz made their first contribution in https://github.com/vllm-project/vllm/pull/50977
* @efschu made their first contribution in https://github.com/vllm-project/vllm/pull/50734
* @fangchenli made their first contribution in https://github.com/vllm-project/vllm/pull/52277
* @fanxingran made their first contribution in https://github.com/vllm-project/vllm/pull/51011
* @fatday made their first contribution in https://github.com/vllm-project/vllm/pull/48171
* @fattchris made their first contribution in https://github.com/vllm-project/vllm/pull/48861
* @fcui-amd made their first contribution in https://github.com/vllm-project/vllm/pull/50126
* @fede-kamel made their first contribution in https://github.com/vllm-project/vllm/pull/50624
* @fxfxfxfxfxfxfxfx made their first contribution in https://github.com/vllm-project/vllm/pull/49139
* @gabriel-peracio made their first contribution in https://github.com/vllm-project/vllm/pull/50183
* @gchinora made their first contribution in https://github.com/vllm-project/vllm/pull/51427
* @guanxingithub made their first contribution in https://github.com/vllm-project/vllm/pull/50589
* @haregali made their first contribution in https://github.com/vllm-project/vllm/pull/50716
* @hsusul made their first contribution in https://github.com/vllm-project/vllm/pull/49613
* @iwannagotobed made their first contribution in https://github.com/vllm-project/vllm/pull/51664
* @jacobzhang22 made their first contribution in https://github.com/vllm-project/vllm/pull/38771
* @jairitAge made their first contribution in https://github.com/vllm-project/vllm/pull/51100
* @jamesETsmith made their first contribution in https://github.com/vllm-project/vllm/pull/51216
* @jimmy-adams made their first contribution in https://github.com/vllm-project/vllm/pull/47972
* @jyan-R made their first contribution in https://github.com/vllm-project/vllm/pull/52311
* @Kaif10 made their first contribution in https://github.com/vllm-project/vllm/pull/52528
* @karen-sy made their first contribution in https://github.com/vllm-project/vllm/pull/48048
* @Lin-z-w made their first contribution in https://github.com/vllm-project/vllm/pull/48069
* @liushujia122 made their first contribution in https://github.com/vllm-project/vllm/pull/51780
* @Luosuu made their first contribution in https://github.com/vllm-project/vllm/pull/48789
* @mispa-ms made their first contribution in https://github.com/vllm-project/vllm/pull/52419
* @mkhazraee made their first contribution in https://github.com/vllm-project/vllm/pull/50321
* @NVShreyas made their first contribution in https://github.com/vllm-project/vllm/pull/50911
* @pavelzak made their first contribution in https://github.com/vllm-project/vllm/pull/52502
* @positive666 made their first contribution in https://github.com/vllm-project/vllm/pull/52329
* @rajfirke made their first contribution in https://github.com/vllm-project/vllm/pull/51573
* @Rapisurazurite made their first contribution in https://github.com/vllm-project/vllm/pull/50950
* @rchalamala made their first contribution in https://github.com/vllm-project/vllm/pull/50487
* @RobbieJ made their first contribution in https://github.com/vllm-project/vllm/pull/49328
* @ruirui6946 made their first contribution in https://github.com/vllm-project/vllm/pull/52098
* @RyanJHamby made their first contribution in https://github.com/vllm-project/vllm/pull/48420
* @samuelkim7 made their first contribution in https://github.com/vllm-project/vllm/pull/50333
* @shanewidanagama made their first contribution in https://github.com/vllm-project/vllm/pull/51901
* @shikamd123 made their first contribution in https://github.com/vllm-project/vllm/pull/45043
* @SilenNaihin made their first contribution in https://github.com/vllm-project/vllm/pull/39935
* @skysnow2001 made their first contribution in https://github.com/vllm-project/vllm/pull/43615
* @Sundaresan-G made their first contribution in https://github.com/vllm-project/vllm/pull/50526
* @syedalijaseem made their first contribution in https://github.com/vllm-project/vllm/pull/47692
* @taking-lying-flat made their first contribution in https://github.com/vllm-project/vllm/pull/51391
* @tandixit95 made their first contribution in https://github.com/vllm-project/vllm/pull/50462
* @Tejas-Raj01 made their first contribution in https://github.com/vllm-project/vllm/pull/49206
* @theminghuang made their first contribution in https://github.com/vllm-project/vllm/pull/51482
* @tobymao made their first contribution in https://github.com/vllm-project/vllm/pull/51318
* @TrainToGPB made their first contribution in https://github.com/vllm-project/vllm/pull/50958
* @tzulingk made their first contribution in https://github.com/vllm-project/vllm/pull/49230
* @UgaTheDev made their first contribution in https://github.com/vllm-project/vllm/pull/51627
* @varoudis made their first contribution in https://github.com/vllm-project/vllm/pull/50404
* @Vegetog made their first contribution in https://github.com/vllm-project/vllm/pull/49876
* @vitamin-chaos made their first contribution in https://github.com/vllm-project/vllm/pull/47106
* @wangxian001 made their first contribution in https://github.com/vllm-project/vllm/pull/50276
* @WillZZZy made their first contribution in https://github.com/vllm-project/vllm/pull/46747
* @xiaopusun made their first contribution in https://github.com/vllm-project/vllm/pull/51495
* @xijiaat made their first contribution in https://github.com/vllm-project/vllm/pull/50693
* @xudonlyu made their first contribution in https://github.com/vllm-project/vllm/pull/51682
* @yifjiang made their first contribution in https://github.com/vllm-project/vllm/pull/51218
* @yu-xin-c made their first contribution in https://github.com/vllm-project/vllm/pull/51652
* @zcxGGmu made their first contribution in https://github.com/vllm-project/vllm/pull/50746
* @ziqifan617 made their first contribution in https://github.com/vllm-project/vllm/pull/51879
* @zobinHuang made their first contribution in https://github.com/vllm-project/vllm/pull/52164

## Contributors
@yewentao256, @AndreasKaratzas, @njhill, @mgoin, @aoshen02, @khluu, @hmellor, @stefankoncarevic, @taneem-ibrahim, @LucasWilkinson, @chaunceyjiang, @jikunshang, @zufangzhu, @bigPYJ1151, @NickLucche, @askliar, @fxmarty-amd, @Rohan138, @gty111, @zhenwei-intel, @Isotr0py, @jeejeelee, @ZJY0516, @wangxiyuan, @BugenZhao, @yma11, @zyongye, @DarkLight1337, @jperezdealgaba, @zhou9402, @connorcarpenter15, @kliuae, @lucifer1004, @Alex-ai-future, @aarushjain29, @TheEpicDolphin, @chaojun-zhang, @pmanczak, @WoosukKwon, @BabyDrangoner, @noooop, @almogtavor, @hongxiayang, @fuscof-ibm, @gau-nernst, @zxd1997066, @tlrmchlsmth, @music-dino, @zexplorerhj, @S1ro1, @jdebache, @sfeng33, @mayuyuace, @ganeshr10, @benchislett, @tzulingk, @gcanlin, @ivanium, @divakar-amd, @R3hankhan123, @sagearc, @vhagor, @ronensc, @micah-wil, @qyYue1389, @vanshbhatia-amd, @hao-aaron, @chengy-sysu, @elvircrn, @taking-lying-flat, @omerpaz95, @Etelis, @vllmellm, @frank-suwen, @KernelClint, @Fangzhou-Ai, @louie-tsai, @simondanielsson, @maxyanghu, @dmai-afk, @KurodaKanbei, @ziqifan617, @bastefaniak, @ECMGit, @haregali, @cmiyai, @fede-kamel, @drakosha, @vineethsaivs, @zcxGGmu, @TQCB, @skysnow2001, @shenoyvvarun, @karen-sy, @fattchris, @RyanJHamby, @shikamd123, @namgyu-youn, @zzt93, @abmfy, @reidliu41, @Rapisurazurite, @tandixit95, @mganczarenko, @yimdev, @anujbolewar, @LiuYinfeng01, @lk-chen, @NVShreyas, @huangzhilin-hzl, @varoudis, @Yejing-Lai, @mkhazraee, @jzakrzew, @TrainToGPB, @waynehacking8, @zixi-qi, @Sundaresan-G, @mindungil, @bitborne, @Wauplin, @jacobzhang22, @zhewenl, @bnellnm, @pisceskkk, @Lin-z-w, @gabriel-peracio, @SilenNaihin, @baodii, @YunzhuLu, @xwu-intel, @BWAAEEEK, @thisjiang, @maobaolong, @anhtra3889, @JaredforReal, @lvhan028, @xiaolong-intel, @andyxning, @cleonard530, @gnovack, @MatthewBonanni, @wangxian001, @lengrongfu, @Tejas-Raj01, @simon-mo, @vitamin-chaos, @arpera, @jairitAge, @jimmy-adams, @ILikeIneine, @woosebastian, @haic0, @edwinlim0919, @fcui-amd, @jhu960213, @jinzhen-lin, @coltonottley, @walterbm, @meenchen, @matteso1, @djramic, @gchinora, @davidjpyu, @tianmu-li, @xiaopusun, @majunze2001, @Vegetog, @puririshi98, @janeyx99, @RobbieJ, @oonyshch, @thegoldenflow, @Srinivasoo7, @fatday, @acheamponge, @efschu, @rajfirke, @fanxingran, @xudonlyu, @lcskrishna, @xijiaat, @GirasoleY, @d4l3k, @samuelkim7, @tarukumar, @acmore, @theminghuang, @khushali9, @wzhao18, @Priyjain-amd, @yiz-liu, @lkm2835, @dmholtz, @Dao007forever, @liushujia122, @LopezCastroRoberto, @UgaTheDev, @tuukkjs, @aditi-amd, @guan404ming, @yiliu30, @zou3519, @Luosuu, @JoursBleu, @varun-sundar-rabindranath, @mpashkovskii, @yu-xin-c, @WillZZZy, @vrdn-23, @xyang16, @ccrhx4, @tanpinsiang, @russellb, @fxfxfxfxfxfxfxfx, @afriedri, @yifjiang, @Akashcodes732, @HF-001, @ovidiusm, @arthurgao2003, @TomerBN-Nvidia, @hotTea123, @vx120, @bohnstingl, @qwerqwerqwe8688-jpg, @jasonozuzu-cohere, @vineetatiwari27, @ruirui6946, @linitra24, @syedalijaseem, @nickus, @yzong-rh, @s3woz, @jhaotingc, @lukealonso, @Jie-Fang, @kzwrime, @xianbaoqian, @velonica0, @ccaadaro, @yisustc, @fangchenli, @iwannagotobed, @zobinHuang, @rchalamala, @shanjiaz, @jamesETsmith, @stacyroberts, @guanxingithub, @biswapanda, @shanewidanagama, @UranusSeven, @hsusul, @tobymao, @mispa-ms, @jeffreywang88, @SayHelloToWorld, @jyan-R, @oops-oom, @shantipriya-amd, @andakai, @akii96, @shen-shanshan, @Kaif10, @yitingdc, @positive666, @pavelzak, @SubSir, @ywang96
