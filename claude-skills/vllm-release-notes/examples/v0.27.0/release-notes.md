# vLLM v0.27.0 Release Notes

## Highlights

This release features 561 commits from 242 contributors (64 new)!

* **Kimi K3 support** with a full stack landing in one release: core model files and kernels (#50089, #50000), Python (#50093) and Rust (#50104) frontends, AttnRes kernels (#50090), DeepGEMM support (#50458), compressed-tensors quantized checkpoints (#50500), DSpark AR fusion (#50242), and an option to shard the shared expert instead of replicating it (#50656).
* **More new models**: Qwen3.5 text-only dense and MoE models (#50210) with EVS video token pruning (#48912), K-EXAONE-2.0-750B-A37B (#50524), VaultGemma via the Transformers modeling backend (#49803), and jina-embeddings-v5-text-nano (#50688).
* **PyTorch 2.13.0 upgrade** along with torchvision 0.28.0 and Triton 3.7.1 (#48155) — this is a breaking environment change; XPU (#48677) and CPU (#50412) followed to torch 2.13 as well.
* **FlashAttention 4 integration deepens on SM100**: FP8 KV cache support (#42569) and headdim-256 support (#42669), backed by a new JIT warmup infrastructure (#47451) and runner-owned Triton kernel warmup (#49903) that remove first-request compilation stalls.
* **DeepSeek-V4 performance push**: sequence parallelism (#46789), ~2x kernel improvement by skipping empty c128 launches (#48957), 3.4% E2E TTFT from skipping unneeded topk/router (#49486), 3.9% E2E TTFT from workspace reuse (#49236), 1.88x kernel from removing a redundant full kernel (#50298), adaptive topk width (1.0% E2E, #50004), 448 MiB GPU memory saved in the PP buffer (#50312), a compact MXFP4 indexer KV cache (#48993), and removal of sparse-MLA q-head padding on FlashInfer >= 0.6.14 (#48047).
* **Model Runner V2 expands to non-generative workloads**: encoder-only attention (#49331), sequence pooling for embedding/classification (#48791), encoder token classification (#50293) and token embedding (#50574), BGE-M3 pooling (#50661), multimodal on CPU (#50073), a multi-layer MTP speculator (#48892), and PCP now selects MRV2 (#50034).
* **Resilient large-scale serving**: a (simplified) fault tolerance framework for DP+EP external load-balancer deployments (#44428) and async preparation for elastic EP scaling (#47288).
* **Disaggregation for hybrid models**: NIXL P/D for hybrid MLA+SSM models (#49762), heterogeneous P/D block sizes for hybrid models (#49612), and MoRIIO heterogeneous TP<->DP prefill/decode read routing (#46116).
* **Rust frontend grows a gRPC control plane**: engine-aware health reporting (#48992), abort control (#49255), server and model discovery (#49491), KV event source discovery (#50033), plus `vllm-bench` integrated into the `vllm` CLI (#48930).
* **Early next-gen hardware enablement**: `sm_107` target for NVIDIA Rubin (#49387) with NVLink all-reduce paths on SM107 (#49647), and ROCm gfx1250 architecture enabled (#46516).

### Model Support
* Kimi K3: new model (#50000) with model files and kernels (#50089), Python frontend (#50093), Rust frontend (#50104), AttnRes kernels (#50090), DeepGEMM support (#50458), DSpark AR fusion (#50242), and optional shared-expert sharding (#50656).
* New models: Qwen3.5 text-only dense and MoE (#50210), K-EXAONE-2.0-750B-A37B (#50524), VaultGemma via Transformers backend (#49803), jina-embeddings-v5-text-nano with EuroBERT encoder backbone (#50688).
* Inkling: llm-compressor NVFP4 weights (#49258) and compressed-tensors dynamic FP8 (#48876).
* Multimodal: VidCom2 video token pruning (#47750), EVS for Qwen3.5 (#48912), ViT CUDA graph for Gemma-4 (#46837), Cosmos3 FP8 ModelOpt/Diffusers remapping (#48952), MiniMax-M3 MSA speculative decode verification (#50032) and default video processor (#50305), DeepSeek-OCR-2 TTFT optimization (#49531), longer max audio duration for MOSS-TD (#49403).
* Diffusion models: top_k and top_p sampling for DiffusionGemma (#45429).
* Transformers modeling backend: audio model support (#39330), improved `fx` tracer (#49957), fused residual-add + RMSNorm compilation pass (#48757), and fixes for MLA padding + grouped topk routing (#49982), MQA with TP (#49987), and Qwen3-VL M-RoPE (#49292).

### Engine Core
* Warmup: new JIT warmup infrastructure (#47451), runner-owned Triton kernels warmed before the first request (#49903), and proper renderer warmup (#50408).
* Attention: FlashAttention 4 SM100 FP8 KV cache (#42569) and headdim-256 (#42669); query replication for MLA decode under DCP for DeepSeek-V2/R1 and Kimi-K2.5 (#45964); masked MHA for sparse MLA prefills (#48770); skip sparse indexer scoring for short dense prefills (#48407); FlexAttention epilogue hook (#45841) and encoder block-mask compile explosion avoided (#50339); attention backends stay eligible for text-only serving of prefix-LM models (#48796); merge-attention context count as a runtime argument (#48739); unified multi-path encoder CUDA graph support (#49934); encoder cache extension hooks (#48218).
* Model Runner V2: encoder-only attention (#49331), sequence pooling for embedding and classification (#48791), encoder token classification (#50293), encoder token embedding (#50574), BGE-M3 pooling (#50661), multimodal on CPU (#50073), multi-layer MTP speculator (#48892), PCP selects MRV2 (#50034), attention metadata always built at capture time (#49364), encoder cache profiling (#47985), skipped no-op FP32 logits materialization (#47711), chunked rejection sampler to avoid OOM (#48630), fewer GPU<->CPU syncs in hybrid Mamba (#49736).
* KV offloading: generic P2P secondary tier with peer lookup/serving (#48021), per-request tier filtering with TierFilter/TierMatcher (#48123), self-describing KV events with TieringOffloadingSpec (#48679), pluggable eviction policies via CachePolicyFactory (#49114), deduplicated replicated MLA KV in the shared CPU region (#48906), single-copy MLA layout for CPUOffloadingSpec (#50301), CPUOffloadingSpec moved onto SharedOffloadRegion (#50094), TP-independent compact secondary identity (#49858), batched C store/load for filesystem offload (#49152), reliable partial-tail offload for sub-block prompts (#49502), per-layer canonical KV page mappings for parallelism-agnostic offload (#48408).
* Mamba/hybrid: ReplaySSM caching for faster Mamba2 standard decode (#48018), fused align-mode DS-conv state migration with num_accepted_tokens > 1 (#49291), FlashInfer Mamba SSU algorithm selection (#50157), fixed `/wake_up` crash on hybrid models (#41602).
* Spec decode: DSpark Markov head replicated across TP ranks (#49731), `sample_from_anchor` loaded from speculators config (#48639), earliest-completing stop string selected (#49391).
* Structured outputs: grammar advanced across the reasoning boundary with spec decode (#44993).
* Preprocessing performance: derender CPU work offloaded to the renderer thread pool (#49396), raw-prompt preprocessing off the event loop in AsyncLLM (#49608), multimodal preprocessing isolated on its own executor (#49524), MM embeds loading deferred off the event loop (#49477), parallel preprocessing within a request for online pooling (#49153), videos hashed by source bytes (#49607), original image mode preserved in ImageIO (#49159).
* RL: weight version tagging for RL rollouts (#49040), stateful trainer-send IPC (#48981), vLLM config set during weight reload (#45989), router replay output from the FlashInfer monolithic MoE kernel (#44214).
* Memory & robustness: CuMem slept-L1 fragmentation accounting (#49208), cgroup memory limits respected on all platforms (#49966), fail fast when /dev/shm is too small (#48879), zero-copy tensor pickling in shm_broadcast (#48442), LRU hash-split skipped in free_blocks when prefix caching is off (#48017), location-derived path vars excluded from torch.compile cache factors (#47573), `CustomOp.forward_native` compiled for ReLU^2 (#50244), HF config used for HF tokenizers (#49907), batch-invariant RMSNorm via pinned block size (#48391).

### Hardware & Performance
* DeepSeek-V4: sequence parallelism (#46789), ~2x kernel skipping empty c128 launches (#48957), 3.4% E2E TTFT skipping topk/router in decode (#49486), 3.9% E2E TTFT workspace reuse (#49236), 1.88x kernel removing a redundant full kernel (#50298), adaptive topk width 1.0% E2E (#50004), 448 MiB GPU memory saved (#50312), compact MXFP4 indexer KV cache (#48993), sparse-MLA q-head padding removed for FlashInfer >= 0.6.14 (#48047).
* Kernels: RMSNorm uncontiguous support with 1.2–3.1x kernel improvement (#49750), MoE `reduce_scatter` regression fix restoring 5% E2E throughput (#48763), non-grouped bias-less topk routing dispatched to the fused path (#49618), tuned LL BF16 router GEMM (#48774) with warmup skipped for non-MoE models (#49659), Triton tensor-descriptor path for fused MoE via `VLLM_TRITON_USE_TD` (#42436), cudagraph/DP padding skipped in topk (#48979), coalesced HBM access in the Marlin INT4-FP8 AWQ preprocess kernel (#47268).
* NVIDIA next-gen: `sm_107` for Rubin (#49387), NVLink all-reduce paths on SM107 (#49647), fixed CUDA arch detection producing kernel-less builds on SM121 (#49904).
* ROCm: gfx1250 architecture enabled (#46516), AITER FP8 ViT encoder attention (#49937), fused shared expert for Quark DeepSeek-V4 checkpoints (#48044), Quark GLM-5.2 checkpoint inference fixes (#48886), DSv3.2 per-decode FillFunctor launches eliminated in the sparse-MLA hot loop (#44527), B-preshuffled attention FP8 projections for DSv4 (#46720), TML Inkling enabled (#48841), tuned selective_state_update float16 config for MI325X (#50006), GPT-J-style MRoPE fixed and optimized (#49906), quickreduce accuracy fix in cudagraph mode (#46913), cached fp32 upcast of static e8m0 weight scales (#47773), batch DMA for CPU KV cache loads (#49843), elastic EP scaling accuracy fix (#47206).
* XPU: QK Norm + RoPE fusion pass (#49394), FP8 o_proj with fp8_bmm and load-time scale transpose (#48334), DeepSeek-V4 fuse_index_q SYCL kernel path (#45991), TD operand loads for batched MoE GEMM (#46340), RMSNorm kernels unified with vllm_c (#46981).
* CPU: INT8 fused MoE kernel for Arm CPUs (#48637), s390x inference optimization with oneDNN INT8 GEMM (#50219), GDN conv path optimized for speculative decoding (#48577), granite-4 enabled (#47641), FAST_EXP for Power (#49571), CPU kernels bumped to the latest version (#50387), macOS build fixes (#49021, #50915).

### Large Scale Serving & Distributed
* Fault tolerance framework (simplified) for DP+EP external LB deployments (#44428).
* Elastic EP: async preparation (#47288) and non-contiguous weight transfer fix (#50641).
* P/D disaggregation: NIXL P/D for hybrid MLA+SSM models (#49762), NIXL heterogeneous P/D block sizes for hybrid models (#49612), MoRIIO heterogeneous TP<->DP prefill/decode read routing (#46116), optional lookup disable on PD decode (#50498), prefill token ids reused on the decode chat path (#48145), detokenization streaming derender (#47301), NixlPush skips an extra handshake step in D->P (#49345).
* Fixes: P/D preemption race (#50297), KV lease deadlines rebased onto the worker clock (#50326), NIXL hybrid MLA+mamba heterogeneous TP (#49297), internal LB load-balancing (#49204).
* Mooncake: vectorized `prepare_value` on the KV load path (#48531), full external hits re-derived on stored boundaries (#49481).
* Encoder-cache connectors: `has_pending_push_work` (#49582).
* Communicators: process-checkpoint lifecycle hooks, starting with FlashInfer (#46877).

### Quantization
* New capabilities: FP4 Qutlass integration for compressed-tensors (#43229), CuTeDSL MoE for ReLU2 NVFP4 (#49580), MXFP8 linear support in INC (#47514), AutoRound W4A16 MoE and MXFP4 linear/MoE on XPU (#47124), KV quant mode for TurboQuant (#50533), ModelOpt FP8 emulation on SM80 (#50019), `--linear-backend` honored for ModelOpt W4A16 (#50273).
* Checkpoints: compressed-tensors support for DeepSeek-V4 (#41276) and Kimi-K3 (#50500); `find_matched_target` prioritizes fused-name matches (#49483).
* MoE refactor: FusedMoE renamed to FusedMoEFactory (#44941), MoeWNA16 migrated to the MK oracle scheme (#44120), Quark w8a8-int8 (#46765) and MXFP4 `aiter`/`emulation` backends (#49348, #48949) moved to kernel abstractions, CT WNA16 Marlin/MoE methods merged (#44570), Quark W4A8 (INT4-FP8) MoE CI coverage (#48050).

### API & Frontend
* Rust frontend: gRPC control plane with engine-aware health reporting (#48992), abort control RPC (#49255), server and model discovery (#49491), and KV event source discovery (#50033); `vllm-bench` integrated into `vllm-rs` and the `vllm` CLI (#48930) with opt-in Rust delegation for `vllm bench serve` (#50081); zero-copy multimodal tensor slicing (#48781), multimodal tensors in auxiliary frames (#49341), `--limit-mm-per-prompt` (#49604), ordinary-text tokenizer encoding (#49992).
* APIs: Cohere chat v2 API support (#47189), `cache_salt` in the Anthropic Messages API (#49498), strict tool calling and constrained decoding for GPT-OSS Harmony (#45560), unified engine-based Mistral parser for reasoning and tool calls (#48947), `stream_interval` exposed as a per-request sampling param (#49754), additional sampling parameters for the translation API (#45839), `diarized_json` for MOSS-Transcribe-Diarize (#48543), cumulative speech-to-text chunk timestamps (#41131).
* Multimodal: mm hash algorithm selection via CLI (#49686), configurable PyNvVideoCodec decoder concurrency (#49753), RFC 2397 parameters accepted in base64 data URLs (#48973).
* UX & validation: standardized request error handling with a VLLMError hierarchy (#49665), reduced startup log noise (#50590), incompatible nested runtime overrides rejected (#49247), improved data-parallel launch validation (#49124), DCP topology validation (#49777), 400 instead of 500 for non-numeric logprobs (#49144), bare Inkling text preserved in Python and Rust parsers (#50403).
* Benchmarks: probe requests in `vllm bench serve` (#49611).

### Dependencies
* PyTorch 2.13.0, torchvision 0.28.0, Triton 3.7.1 (#48155); torch 2.13 for XPU (#48677) and CPU (#50412).
* Transformers 5.14.1 (#49223), FlashInfer 0.6.15 (#48914) then 0.6.16.post3 (#50892), AITER 0.1.16.post5 (#48683) then 0.1.19 (#49361), NCCL 2.30.7 enabling DeepEPv2 in the vllm/vllm-openai image (#45321), tpu-inference v0.25.0 (#49431) then v0.26.0 (#50522), Helion 1.4.0 (#50307), NIXL and UCX upgraded on ROCm (#49251).
* Build: vllm-flash-attn bumped to a C++20-compatible commit for torch-nightly (#49326), ABI-stable FA2 build pin (#50474).

### Deprecations & Removals
* Models removed: Plamo2 (#49729), Ouro (#49786).
* Removed the no-longer-supported `max_num_partial_prefills` and `max_long_partial_prefills` arguments (#49244).

## New Contributors

* @afriedri made their first contribution in https://github.com/vllm-project/vllm/pull/49621
* @amd-sourjya made their first contribution in https://github.com/vllm-project/vllm/pull/48050
* @andreatassi made their first contribution in https://github.com/vllm-project/vllm/pull/48879
* @andrewbcohere made their first contribution in https://github.com/vllm-project/vllm/pull/47189
* @avininjamay8 made their first contribution in https://github.com/vllm-project/vllm/pull/47764
* @bastefaniak made their first contribution in https://github.com/vllm-project/vllm/pull/49190
* @boe20211 made their first contribution in https://github.com/vllm-project/vllm/pull/49431
* @bugkeep made their first contribution in https://github.com/vllm-project/vllm/pull/41357
* @cagrikymk made their first contribution in https://github.com/vllm-project/vllm/pull/46720
* @chun-wan made their first contribution in https://github.com/vllm-project/vllm/pull/44972
* @ColinZ22 made their first contribution in https://github.com/vllm-project/vllm/pull/48044
* @dsocek made their first contribution in https://github.com/vllm-project/vllm/pull/47791
* @euisuh made their first contribution in https://github.com/vllm-project/vllm/pull/49654
* @evantakahashi made their first contribution in https://github.com/vllm-project/vllm/pull/47953
* @FeathBow made their first contribution in https://github.com/vllm-project/vllm/pull/49111
* @fxmarty made their first contribution in https://github.com/vllm-project/vllm/pull/49732
* @hotTea123 made their first contribution in https://github.com/vllm-project/vllm/pull/48218
* @Ibrahim2595 made their first contribution in https://github.com/vllm-project/vllm/pull/45432
* @jcotant-inferact made their first contribution in https://github.com/vllm-project/vllm/pull/49474
* @jiacao-amd made their first contribution in https://github.com/vllm-project/vllm/pull/47773
* @Johnny-Liou made their first contribution in https://github.com/vllm-project/vllm/pull/48018
* @johnnyychiu made their first contribution in https://github.com/vllm-project/vllm/pull/45532
* @jongukc made their first contribution in https://github.com/vllm-project/vllm/pull/49440
* @kevglynn made their first contribution in https://github.com/vllm-project/vllm/pull/41602
* @krishnateja95 made their first contribution in https://github.com/vllm-project/vllm/pull/48876
* @Lafunamor made their first contribution in https://github.com/vllm-project/vllm/pull/40289
* @latent-9 made their first contribution in https://github.com/vllm-project/vllm/pull/50491
* @LG-0927 made their first contribution in https://github.com/vllm-project/vllm/pull/50571
* @liminfei-amd made their first contribution in https://github.com/vllm-project/vllm/pull/48739
* @lkk12014402 made their first contribution in https://github.com/vllm-project/vllm/pull/47124
* @loulanyue made their first contribution in https://github.com/vllm-project/vllm/pull/50704
* @markyangcc made their first contribution in https://github.com/vllm-project/vllm/pull/49021
* @meiyeh123 made their first contribution in https://github.com/vllm-project/vllm/pull/50522
* @Mi-Jiazhi made their first contribution in https://github.com/vllm-project/vllm/pull/49691
* @microslaw made their first contribution in https://github.com/vllm-project/vllm/pull/47298
* @molly-ting made their first contribution in https://github.com/vllm-project/vllm/pull/49660
* @neweyes made their first contribution in https://github.com/vllm-project/vllm/pull/49659
* @nickus made their first contribution in https://github.com/vllm-project/vllm/pull/48366
* @nikhilkulkarni1755 made their first contribution in https://github.com/vllm-project/vllm/pull/44239
* @nvbfalk made their first contribution in https://github.com/vllm-project/vllm/pull/47750
* @omkar-droid made their first contribution in https://github.com/vllm-project/vllm/pull/50688
* @oops-oom made their first contribution in https://github.com/vllm-project/vllm/pull/49985
* @ormandj made their first contribution in https://github.com/vllm-project/vllm/pull/48317
* @PerkzZheng made their first contribution in https://github.com/vllm-project/vllm/pull/50210
* @philippesic made their first contribution in https://github.com/vllm-project/vllm/pull/49114
* @qtris123 made their first contribution in https://github.com/vllm-project/vllm/pull/48796
* @RyanClark2k made their first contribution in https://github.com/vllm-project/vllm/pull/48438
* @samlaf made their first contribution in https://github.com/vllm-project/vllm/pull/50200
* @ShuoleiWang made their first contribution in https://github.com/vllm-project/vllm/pull/49040
* @siddhant-bharti made their first contribution in https://github.com/vllm-project/vllm/pull/50065
* @Spycsh made their first contribution in https://github.com/vllm-project/vllm/pull/47871
* @stacyroberts made their first contribution in https://github.com/vllm-project/vllm/pull/47207
* @stefan-kaestle made their first contribution in https://github.com/vllm-project/vllm/pull/49177
* @thomas-fahrner-parasail made their first contribution in https://github.com/vllm-project/vllm/pull/48973
* @TobyB1702 made their first contribution in https://github.com/vllm-project/vllm/pull/41131
* @vecheruk-amd made their first contribution in https://github.com/vllm-project/vllm/pull/38293
* @wangqia0309 made their first contribution in https://github.com/vllm-project/vllm/pull/48563
* @wkutak made their first contribution in https://github.com/vllm-project/vllm/pull/48952
* @wskr00 made their first contribution in https://github.com/vllm-project/vllm/pull/49073
* @xuanyu-mistral made their first contribution in https://github.com/vllm-project/vllm/pull/44214
* @yamt made their first contribution in https://github.com/vllm-project/vllm/pull/50547
* @yuan-alex made their first contribution in https://github.com/vllm-project/vllm/pull/48917
* @yudigege86 made their first contribution in https://github.com/vllm-project/vllm/pull/50476
* @zaristei made their first contribution in https://github.com/vllm-project/vllm/pull/49647

## Contributors

@AndreasKaratzas, @njhill, @hmellor, @mgoin, @BugenZhao, @yewentao256, @khluu, @taneem-ibrahim, @guan404ming, @NickLucche, @stefankoncarevic, @Change72, @zhenwei-intel, @reidliu41, @oonyshch, @Isotr0py, @MatthewBonanni, @andyxning, @WoosukKwon, @tlrmchlsmth, @ZJY0516, @fxmarty-amd, @ivanium, @aoshen02, @xwu-intel, @connorcarpenter15, @lengrongfu, @bnellnm, @mikekg, @kylesayrs, @aarushjain29, @fadara01, @netanel-haber, @Etelis, @LopezCastroRoberto, @jikunshang, @chaunceyjiang, @umut-polat, @divakar-amd, @gau-nernst, @ColinZ22, @jcotant-inferact, @R3hankhan123, @chaojun-zhang, @jongukc, @zxd1997066, @bigPYJ1151, @yzong-rh, @hickeyma, @yushangdi, @majunze2001, @Akashcodes732, @zufangzhu, @sagearc, @FeathBow, @xiaolong-intel, @coltonottley, @okorzh-amd, @sungsooha, @cleonard530, @microslaw, @vllm-agent, @LucasWilkinson, @eicherseiji, @Fangzhou-Ai, @peizhang56, @ZeldaHuang, @noooop, @atalman, @wzhao18, @matteso1, @GirasoleY, @Dao007forever, @chaeminlim-mb, @music-dino, @amd-sourjya, @BadrBasowid, @Johnny-Liou, @Rohan138, @brandonpelfrey, @harjothkhara, @tianmu-li, @fxmarty, @oops-oom, @TheEpicDolphin, @yma11, @itayalroy, @shen-shanshan, @JaredforReal, @wskr00, @janeyx99, @mayuyuace, @omkar-droid, @mganczarenko, @Spycsh, @hclsys, @EdalatiAli, @wangqia0309, @tc-mb, @wkutak, @charlifu, @fynnsu, @xuanyu-mistral, @ormandj, @flutist, @simon-mo, @yuan-alex, @esmeetu, @stefan-kaestle, @bastefaniak, @markyangcc, @ilmarkov, @jjmiao1, @stecasta, @evantakahashi, @ariG23498, @sfeng33, @rasmith, @gnovack, @boe20211, @bbrowning, @S1ro1, @davidjpyu, @nikhilkulkarni1755, @vllmellm, @yuyue0225sc, @euisuh, @zhou9402, @li-jinpeng, @hotTea123, @mgazz, @thomas-fahrner-parasail, @elvircrn, @djramic, @adobrzyn, @qtris123, @harshaljanjani, @mosya415, @gcanlin, @fangyuchu, @anthonsu, @Palaiologos1453, @Achyuthan-S, @athrael-soju, @LiuLi1998, @liranschour, @Alex-ai-future, @dsocek, @galletas1712, @nickus, @edwinlim0919, @walterbm, @thegoldenflow, @Zhenzhong1, @haoyangli0109, @tjtanaa, @ronensc, @neweyes, @liminfei-amd, @garrygale, @jesse996, @TobyB1702, @puririshi98, @andreatassi, @avininjamay8, @frida-andersson, @nvbfalk, @varun-sundar-rabindranath, @mindungil, @ayush1399, @afriedri, @ShuoleiWang, @Liangliang-Ma, @xin3he, @liangel-02, @Ibrahim2595, @qianlihuang, @jiacao-amd, @brian-dellabetta, @labAxiaoming, @johnnyychiu, @siddhant-bharti, @jdebache, @mrn3088, @kevglynn, @krishnateja95, @danielafrimi, @zqzten, @philippesic, @afierka-intel, @PerkzZheng, @omerpaz95, @cinnamonica02, @fallintoplace, @yintong-lu, @chanh, @roikoren755, @zaristei, @bugkeep, @vecheruk-amd, @molly-ting, @lkk12014402, @LiuYinfeng01, @jperezdealgaba, @stacyroberts, @juliendenize, @hao-aaron, @cagrikymk, @zou3519, @RyanClark2k, @samlaf, @jpvillam-amd, @mawong-amd, @vanshbhatia-amd, @rjrock, @majian4work, @woosebastian, @Mi-Jiazhi, @jeejeelee, @latent-9, @oguzhankir, @LG-0927, @ylangtsou, @meiyeh123, @skavulya, @Lafunamor, @Amir-19, @andrewbcohere, @yudigege86, @aeon-x, @wentian-byte, @almogtavor, @hongxiayang, @loulanyue, @jasonlizhengjian, @chun-wan, @wjabbour, @yamt, @amitz-nv, @JianDan0212, @lkm2835, @lk-chen
