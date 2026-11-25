# vLLM Past Release Notes Highlights

## v0.11.0 (2025-10-02)
## Highlights
This release features 538 commits, 207 contributors (65 new contributors)!

* This release completes the removal of V0 engine. V0 engine code including AsyncLLMEngine, LLMEngine, MQLLMEngine, all attention backends, and related components have been removed. **V1 is the only engine in the codebase now.**
* This releases turns on **FULL_AND_PIECEWISE as the CUDA graph mode default**. This should provide better out of the box performance for most models, particularly fine-grained MoEs, while preserving compatibility with existing models supporting only PIECEWISE mode.

Note: In v0.11.0 (and v0.10.2), `--async-scheduling` will produce gibberish output in some cases such as preemption and others. This functionality is correct in v0.10.1. We are actively fixing it for the next version. 

### Model Support
* New architectures: DeepSeek-V3.2-Exp (#25896), Qwen3-VL series (#24727), Qwen3-Next (#24526), OLMo3 (#24534), LongCat-Flash (#23991), Dots OCR (#24645), Ling2.0 (#24627), CWM (#25611).
* Encoders: RADIO encoder support (#24595), Transformers backend support for encoder-only models (#25174).
* Task expansion: BERT token classification/NER (#24872), multimodal models for pooling tasks (#24451).
* Data parallel for vision encoders: InternVL (#23909), Qwen2-VL (#25445), Qwen3-VL (#24955).
* Speculative decoding: EAGLE3 for MiniCPM3 (#24243) and GPT-OSS (#25246).
* Features: Qwen3-VL text-only mode (#26000), EVS video token pruning (#22980), Mamba2 TP+quantization (#24593), MRoPE + YaRN (#25384), Whisper on XPU (#25123), LongCat-Flash-Chat tool calling (#24083).
* Performance: GLM-4.1V 916ms TTFT reduction via fused RMSNorm (#24733), GLM-4 MoE SharedFusedMoE optimization (#24849), Qwen2.5-VL CUDA sync removal (#24741), Qwen3-VL Triton MRoPE kernel (#25055), FP8 checkpoints for Qwen3-Next (#25079).
* Reasoning: SeedOSS reason parser (#24263).

### Engine Core
* KV cache offloading: CPU offloading with LRU management (#19848, #20075, #21448, #22595, #24251).
* V1 features: Prompt embeddings (#24278), sharded state loading (#25308), FlexAttention sliding window (#24089), LLM.apply_model (#18465).
* Hybrid allocator: Pipeline parallel (#23974), varying hidden sizes (#25101).
* Async scheduling: Uniprocessor executor support (#24219).
* Architecture: Tokenizer group removal (#24078), shared memory multimodal caching (#20452).
* Attention: Hybrid SSM/Attention in Triton (#21197), FlashAttention 3 for ViT (#24347).
* Performance: FlashInfer RoPE 2x speedup (#21126), fused Q/K RoPE 11% improvement (#24511, #25005), 8x spec decode overhead reduction (#24986), FlashInfer spec decode with 1.14x speedup (#25196), model info caching (#23558), inputs_embeds copy avoidance (#25739).
* LoRA: Optimized weight loading (#25403).
* Defaults: CUDA graph mode FULL_AND_PIECEWISE (#25444), Inductor standalone compile disabled (#25391).
* torch.compile: CUDA graph Inductor partition integration (#24281).

### Hardware & Performance
* NVIDIA: FP8 FlashInfer MLA decode (#24705), BF16 fused MoE for Hopper/Blackwell expert parallel (#25503).
* DeepGEMM: Enabled by default (#24462), 5.5% throughput improvement (#24783).
* New architectures: RISC-V 64-bit (#22112), ARM non-x86 CPU (#25166), ARM 4-bit fused MoE (#23809).
* AMD: ROCm 7.0 (#25178), GLM-4.5 MI300X tuning (#25703).
* Intel XPU: MoE DP accuracy fix (#25465).

### Large Scale Serving & Performance
* Dual-Batch Overlap (DBO): Overlapping computation mechanism (#23693), DeepEP high throughput + prefill (#24845).
* Data Parallelism: torchrun launcher (#24899), Ray placement groups (#25026), Triton DP/EP kernels (#24588).
* EPLB: Hunyuan V1 (#23078), Mixtral (#22842), static placement (#23745), reduced overhead (#24573).
* Disaggregated serving: KV transfer metrics (#22188), NIXL MLA latent dimension (#25902).
* MoE: Shared expert overlap optimization (#24254), SiLU kernel for DeepSeek-R1 (#24054), Enable Allgather/ReduceScatter backend for NaiveAllToAll (#23964).
* Distributed: NCCL symmetric memory with 3-4% throughput improvement (#24532), enabled by default for TP (#25070).

### Quantization
* FP8: Per-token-group quantization (#24342), hardware-accelerated instructions (#24757), torch.compile KV cache (#22758), paged attention update (#22222).
* FP4: NVFP4 for dense models (#25609), Gemma3 (#22771), Llama 3.1 405B (#25135).
* W4A8: Faster preprocessing (#23972).
* Compressed tensors: Blocked FP8 for MoE (#25219).

### API & Frontend
* OpenAI: Prompt logprobs for all tokens (#24956), logprobs=-1 for full vocab (#25031), reasoning streaming events (#24938), Responses API MCP tools (#24628, #24985), health 503 on dead engine (#24897).
* Multimodal: Media UUID caching (#23950), image path format (#25081).
* Tool calling: XML parser for Qwen3-Coder (#25028), Hermes-style tokens (#25281).
* CLI: --enable-logging (#25610), improved --help (#24903).
* Config: Speculative model engine args (#25250), env validation (#24761), NVTX profiling (#25501), guided decoding backward compatibility (#25615, #25422).
* Metrics: V1 TPOT histogram (#24015), hidden deprecated gpu_ metrics (#24245), KV cache GiB units (#25204, #25479).
* UX: Removed misleading quantization warning (#25012).

### Security
* https://github.com/vllm-project/vllm/security/advisories/GHSA-wr9h-g72x-mwhm

### Dependencies
* PyTorch 2.8 for CPU (#25652), FlashInfer 0.3.1 (#24470), CUDA 13 (#24599), ROCm 7.0 (#25178).
* **Build requirements**: C++17 now enforced globally (#24823).
* **TPU**: Deprecated `xm.mark_step` in favor of `torch_xla.sync` (#25254).

### V0 Deprecation
* Engines: AsyncLLMEngine (#25025), LLMEngine (#25033), MQLLMEngine (#25019), core (#25321), model runner (#25328), MP executor (#25329).
* Components: Attention backends (#25351), encoder-decoder (#24907), output processor (#25320), sampling metadata (#25345), Sequence/Sampler (#25332).
* Interfaces: LoRA (#25686), async output processor (#25334), MultiModalPlaceholderMap (#25366), seq group methods (#25330), placeholder attention (#25510), input embeddings (#25242), multimodal registry (#25362), max_seq_len_to_capture (#25543), attention classes (#25541), hybrid models (#25400), backend suffixes (#25489), compilation fallbacks (#25675), default args (#25409).


## v0.10.2 
## Highlights
This release contains 740 commits from 266 contributors (97 new)!

**Breaking Changes**: This release includes PyTorch 2.8.0 upgrade, V0 deprecations, and API changes - please review the changelog carefully.

**aarch64 support**: This release features native support for aarch64 allowing usage of vLLM on GB200 platform. The docker image `vllm/vllm-openai` should already be multiplatform. To install the wheels, you can download the wheels from this release artifact or install via 
```
uv pip install vllm==0.10.2 --extra-index-url https://wheels.vllm.ai/0.10.2/ --torch-backend=auto
```

### Model Support
* **New model families and enhancements**: Apertus (#23068), LFM2 (#22845), MiDashengLM (#23652), Motif-1-Tiny (#23414), Seed-Oss (#23241), Google EmbeddingGemma-300m (#24318), GTE sequence classification (#23524), Donut OCR model (#23229), KeyeVL-1.5-8B (#23838), R-4B vision model (#23246), Ernie4.5 VL (#22514), MiniCPM-V 4.5 (#23586), Ovis2.5 (#23084), Qwen3-Next with hybrid attention (#24526), InternVL3.5 with video support (#23658), Qwen2Audio embeddings (#23625), NemotronH Nano VLM (#23644), BLOOM V1 engine support (#23488), and Whisper encoder-decoder for V1 (#21088).
* **Pipeline parallelism expansion**: Added PP support for Hunyuan (#24212), Ovis2.5 (#23405), GPT-OSS (#23680), and Kimi-VL-A3B-Thinking-2506 (#23114).
* **Data parallelism for vision models**: Enabled DP for ViT across Qwen2.5VL (#22742), MiniCPM-V (#23948, #23327), Kimi-VL (#23817), and GLM-4.5V (#23168).
* **LoRA ecosystem expansion**: Added LoRA support to Voxtral (#24517), Qwen-2.5-Omni (#24231), and DeepSeek models V2/V3/R1-0528 (#23971), with significantly faster LoRA startup performance (#23777).
* **Classification and pooling enhancements**: Multi-label classification support (#23173), logit bias and sigmoid normalization (#24031), and FP32 precision heads for pooling models (#23810).
* **Performance optimizations**: Removed unnecessary CUDA sync from GLM-4.1V (#24332) and Qwen2VL (#24334) preprocessing, eliminated redundant all-reduce in Qwen3 MoE (#23169), optimized InternVL CPU threading (#24519), and GLM4.5-V video frame decoding (#24161).

### Engine Core
* **V1 engine maturation**: Extended V1 support to compute capability < 8.0 (#23614, #24022), added cross-attention KV cache for encoder-decoder models (#23664), request-level logits processor integration (#23656), and KV events from connectors (#19737).
* **Backend expansion**: Terratorch backend integration (#23513) enabling non-language model tasks like semantic segmentation and geospatial applications with `--model-impl terratorch` support.
* **Hybrid and Mamba model improvements**: Enabled full CUDA graphs by default for hybrid models (#22594), disabled prefix caching for hybrid/Mamba models (#23716), added FP32 SSM kernel support (#23506), full CUDA graph support for Mamba1 (#23035), and V1 as default for Mamba models (#23650).
* **Performance core improvements**: `--safetensors-load-strategy` for NFS based file loading acceleration (#24469), critical CUDA graph capture throughput fix (#24128), scheduler optimization for single completions (#21917), multi-threaded model weight loading (#23928), and tensor core usage enforcement for FlashInfer decode (#23214).
* **Multimodal enhancements**: Multimodal cache tracking with mm_hash (#22711), UUID-based multimodal identifiers (#23394), improved V1 video embedding estimation (#24312), and simplified multimodal UUID handling (#24271).
* **Sampling and structured outputs**: Support for all prompt logprobs (#23868), final logprobs (#22387), grammar bitmask optimization (#23361), and user-configurable KV cache memory size (#21489).
* **Distributed**: Support Decode Context Parallel (DCP) for MLA (#23734)

### Hardware & Performance
* **NVIDIA Blackwell/SM100 generation**: FP8 MLA support with CUTLASS backend (#23289), DeepGEMM Linear with 1.5% E2E throughput improvement (#23351), Hopper DeepGEMM E8M0 for DeepSeekV3.1 (#23666), SM100 FlashInfer CUTLASS MoE FP8 backend (#22357), MXFP4 fused CUTLASS MoE (#23696), default MXFP4 MoE on Blackwell (#23008), and GPT-OSS DP/EP support with 52,003 tokens/s throughput (#23608).
* **Breaking change**: FlashMLA disabled on Blackwell GPUs due to compatibility issues (#24521).
* **Kernel and attention optimizations**: FlashAttention MLA with CUDA graph support (#14258, #23958), V1 cross-attention support (#23297), FP8 support for FlashMLA (#22668), fused grouped TopK for MoE (#23274), Flash Linear Attention kernels (#24518), and W4A8 support on Hopper (#23198).
* **Performance improvements**: 13.7x speedup for token conversion (#20413), TTIT/TTFT improvements for disaggregated serving (#22760), symmetric memory all-reduce by default (#24111), FlashInfer warmup during startup (#23439), V1 model execution overlap (#23569), and various Triton configuration tuning (#23748, #23939).
* **Platform expansion**: Apple Silicon bfloat16 support for M2+ (#24129), IBM Z V1 engine support (#22725), Intel XPU torch.compile (#22609), XPU MoE data parallelism (#22887), XPU Triton attention (#24149), XPU FP8 quantization (#23148), and ROCm pipeline parallelism with Ray (#24275).
* **Model-specific optimizations**: Hardware-tuned MoE configurations for Qwen3-Next on B200/H200/H100 (#24698, #24688, #24699, #24695), GLM-4.5-Air-FP8 B200 configs (#23695), Kimi K2 optimization (#24597), and QWEN3 Coder/Thinking configs (#24266, #24330).

### Quantization
* **New quantization capabilities**: Per-layer quantization routing (#23556), GGUF quantization with layer skipping (#23188), NFP4+FP8 MoE support (#22674), W4A8 channel scales (#23570), and AMD CDNA2/CDNA3 FP4 support (#22527).
* **Advanced quantization infrastructure**: Compressed tensors transforms for linear operations (#22486) enabling techniques like SpinQuantR1R2R4 and QuIP quantization methods.
* **FlashInfer quantization integration**: FP8 KV cache for TRTLLM prefill attention (#24197), FP8-qkv attention kernels (#23647), and FP8 per-tensor GEMMs (#22895).
* **Platform-specific quantization**: ROCm TorchAO quantization enablement (#24400) and TorchAO module swap configuration (#21982).
* **Performance optimizations**: MXFP4 MoE loading cache optimization (#24154) and compressed tensors version updates (#23202).
* **Breaking change**: Removed original Marlin quantization format (#23204).

### API & Frontend
* **OpenAI API enhancements**: Gemma3n audio transcription/translation endpoints (#23735), transcription response usage statistics (#23576), and return_token_ids parameter (#22587).
* **Response API improvements**: Streaming support for non-harmony responses (#23741), non-streaming logprobs (#23319), MCP tool background mode (#23494), MCP streaming+background support (#23927), and tool output token reporting (#24285).
* **Frontend optimizations**: Error stack traces with --log-error-stack (#22960), collective RPC endpoint (#23075), beam search concurrency optimization (#23599), unnecessary detokenization skipping (#24236), and custom media UUIDs (#23449).
* **Configuration enhancements**: Formalized --mm-encoder-tp-mode flag (#23190), VLLM_DISABLE_PAD_FOR_CUDAGRAPH environment variable (#23595), EPLB configuration parameter (#20562), embedding endpoint chat request support (#23931), and LM Format Enforcer V1 integration (#22564).

### Dependencies
* **Major updates**: PyTorch 2.8.0 upgrade (#20358) - breaking change requiring environment updates, FlashInfer v0.3.0 upgrade (#24086), and FlashInfer 0.2.14.post1 maintenance update (#23537).
* **Supporting updates**: XGrammar 0.1.23 (#22988), TPU core dump fix with tpu_info 0.4.0 (#23135), and compressed tensors version bump (#23202).
* **Deployment improvements**: FlashInfer cubin directory environment variable (#22675) for offline environments and pre-cached CUDA binaries.

### V0 Deprecation
* **Backend removals**: V0 Neuron backend deprecation (#21159), V0 pooling model support removal (#23434), V0 FlashInfer attention backend removal (#22776), and V0 test cleanup (#23418, #23862).
* **API breaking changes**: prompt_token_ids fallback removal from LLM.generate and LLM.embed (#18800), LoRA extra vocab size deprecation warning (#23635), LoRA bias parameter deprecation (#24339), and metrics naming change from TPOT to ITL (#24110).

### Breaking Changes
1. **PyTorch 2.8.0 upgrade** - Environment dependency change requiring updated CUDA versions
2. **FlashMLA Blackwell restriction** - FlashMLA disabled on Blackwell GPUs due to compatibility issues
3. **V0 feature removals** - Neuron backend, pooling models, FlashInfer attention backend
5. **Quantizations** - Removed quantized Mixtral hack implementation, and original Marlin format. 
6. **Metrics renaming** - TPOT deprecated in favor of ITL




## v0.10.0 (2025-07-23)
## Highlights
v0.10.0 release includes 308 commits, 168 contributors (62 new!).

**NOTE: This release begins the cleanup of V0 engine codebase.** We have removed V0 CPU/XPU/TPU/HPU backends (#20412), long context LoRA (#21169), Prompt Adapters (#20588), Phi3-Small & BlockSparse Attention (#21217), and Spec Decode workers (#21152) so far and plan to continued to delete code that is no longer used. 

### Model Support
* New families: Llama 4 with EAGLE support (#20591), EXAONE 4.0 (#21060), Microsoft Phi-4-mini-flash-reasoning (#20702), Hunyuan V1 Dense + A13B with reasoning/tool parsing (#21368, #20625, #20820), Ling MoE models (#20680), JinaVL Reranker (#20260), Nemotron-Nano-VL-8B-V1 (#20349), Arcee (#21296), Voxtral (#20970).
* Enhanced compatibility: BERT/RoBERTa with AutoWeightsLoader (#20534), HF format support for MiniMax (#20211), Gemini configuration (#20971), GLM-4 updates (#20736).
* Architecture expansions: Attention-free model support (#20811), Hybrid SSM/Attention models on V1 (#20016), LlamaForSequenceClassification (#20807), expanded Mamba2 layer support (#20660).
* VLM improvements: VLM support with transformers backend (#20543), PrithviMAE on V1 engine (#20577).

### Engine Core
* Experimental async scheduling `--async-scheduling` flag to overlap engine core scheduling with GPU runner (#19970).
* V1 engine improvements: backend-agnostic local attention (#21093), MLA FlashInfer ragged prefill (#20034), hybrid KV cache with local chunked attention (#19351).
* Multi-task support: models can now support multiple tasks (#20771), multiple poolers (#21227), and dynamic pooling parameter configuration (#21128).
* RLHF Support: new RPC methods for runtime weight reloading (#20096) and config updates (#20095), logprobs mode for selecting which stage of logprobs to return (#21398).
* Enhanced caching: multi-modal caching for transformers backend (#21358), reproducible prefix cache hashing using SHA-256 + CBOR (#20511).
* Startup time reduction via CUDA graph capture speedup via frozen GC (#21146).
* Elastic expert parallel for dynamic GPU scaling while preserving state (#20775).

### Hardwares & Performance
* NVIDIA Blackwell/SM100 optimizations: CUTLASS block scaled group GEMM for smaller batches (#20640), FP8 groupGEMM support (#20447), DeepGEMM integration (#20087), FlashInfer MoE blockscale FP8 backend (#20645), CUDNN prefill API for MLA (#20411), Triton Fused MoE kernel config for FP8 E=16 on B200 (#20516).
* Performance improvements: 48% request duration reduction via microbatch tokenization for concurrent requests (#19334), fused MLA QKV + strided layernorm (#21116), Triton causal-conv1d for Mamba models (#18218).
* Hardware expansion: ARM CPU int8 quantization (#14129), PPC64LE/ARM V1 support (#20554), Intel XPU ray distributed execution (#20659), shared-memory pipeline parallel for CPU (#21289), FlashInfer ARM CUDA support (#21013).

### Quantization
* New quantization support: MXFP4 for MoE models (#17888), BNB support for Mixtral and additional MoE models (#20893, #21100), in-flight quantization for MoE (#20061).
* Hardware-specific: FP8 KV cache quantization on TPU (#19292), FP8 support for BatchedTritonExperts (#18864), optimized INT8 vectorization kernels (#20331).
* Performance optimizations: Triton backend for DeepGEMM per-token group quantization (#20841), CUDA kernel for per-token group quantization (#21083), CustomOp abstraction for FP8 (#19830).

### API & Frontend
* OpenAI compatibility: Responses API implementation (#20504, #20975), image object support in llm.chat (#19635), tool calling with required choice and $defs (#20629).
* New endpoints: `get_tokenizer_info` for tokenizer/chat-template information (#20575), cache_salt support for completions/responses (#20981).
* Model loading: Tensorizer S3 integration with arbitrary arguments (#19619), HF repo paths & URLs for GGUF models (#20793), tokenization_kwargs for embedding truncation (#21033).
* CLI improvements: `--help=page` option for enhanced help documentation (#20961), default model changed to Qwen3-0.6B (#20335).

### Dependencies
* Updated PyTorch to 2.7.1 for CUDA (#21011)
* FlashInfer updated to v0.2.8rc1 (#20718)



## v0.9.2 (2025-07-07)

**NOTE: This is the last version where V0 engine code and features stay intact. We highly recommend migrating to V1 engine.**

### Engine Core
* Priority Scheduling is now implemented in V1 engine (#19057), embedding models in V1 (#16188),  Mamba2 in V1 (#19327). 
* Full CUDA Graph execution is now available for all FlashAttention v3 (FA3) and FlashMLA paths, including prefix caching. CUDA graph now has a live capture progress bar makes debugging easier (#20301, #18581, #19617, #19501).  
* FlexAttention update  any head size, FP32 fallback (#20467, #19754).  
* Shared `CachedRequestData` objects and cached sampler ID stores deliver perf enhancements (#20232, #20291).  

### Model Support
* New families: Ernie 4.5 (+MoE) (#20220), MiniMax M1 (#19677, #20297), Slim MoE "Phi tiny MoE instruct" (#20286), Tencent HunYuan MoE V1 (#20114), Keye VL 8B Preview (#20126), GLM 4.1 V (#19331), Gemma 3 (text only, #20134), Tarsier 2 (#19887), Qwen 3 Embedding & Reranker (#19260), dots1 (#18254), GPT 2 for Sequence Classification (#19663).  
* Granite hybrid MoE configurations with shared experts are fully supported (#19652).  

### Large Scale Serving & Engine Improvements
* Expert Parallel Load Balancer (EPLB) has been added! (#18343, #19790, #19885).  
* Disaggregated serving enhancements: Avoid stranding blocks in P when aborted in D's waiting queue (#19223), let toy proxy handle /chat/completions (#19730) 
* Native xPyD P2P NCCL transport as a base case for native PD without external dependency (#18242, #20246).  

### Hardware & Performance
* NVIDIA Blackwell 
	* SM120: CUTLASS W8A8/FP8 kernels and related tuning, added to Dockerfile (#17280, #19566, #20071, #19794) 
	* SM100: block scaled group GEMM, INT8/FP8 vectorization, deep GEMM kernels, activation chunking for MoE, and group size 64 for Machete (#19757, #19572, #19168, #19085, #20290, #20331).  
* Intel GPU (V1) backend with Flash Attention support (#19560).  
* AMD ROCm: full graph capture for TritonAttention, quick All Reduce, and chunked pre fill (#19158, #19744, #18596).  
	* Split KV support landed in the unified Triton Attention kernel, boosting long context throughput (#19152).  
	* Full graph mode enabled in ROCm AITER MLA V1 decode path (#20254).  
* TPU: dynamic grid KV cache updates, head dim less than 128, tuned paged attention kernels, and KV padding fixes (#19928, #20235, #19620, #19813, #20048, #20339).  
	* Add models and features supporting matrix. (#20230) 

### Quantization
* Calibration free RTN INT4/INT8 pipeline for effortless, accurate compression (#18768).  
* Compressed Tensor NVFP4 (including MoE) + emulation; FP4 emulation removed on < SM100 devices (#19879, #19990, #19563).  
* Dynamic MoE layer quant (Marlin/GPTQ) and INT8 vectorization primitives (#19395, #20331, #19233).  
* Bits and Bytes 0.45 + with improved double quant logic and AWQ quality (#20424, #20033, #19431, #20076).

### API � CLI � Frontend
* API Server: Eliminate api_key and x_request_id headers middleware overhead (#19946) 
* New OpenAI compatible endpoints: `/v1/audio/translations` & revamped `/v1/audio/transcriptions` (#19615, #20179, #19597).  
* Token level progress bar for `LLM.beam_search` and cached template resolution speed ups (#19301, #20065).  
* Image object support in `llm.chat`, tool choice expansion, and custom arg passthroughs enrich multi modal agents (#19635, #17177, #16862).  
* CLI QoL: better parsing for `-O/--compilation-config`, batch size sweep benchmarking, richer `--help`, faster startup (#20156, #20516, #20430, #19941).
* Metrics: Deprecate metrics with gpu_ prefix for non GPU specific metrics (#18354), Export NaNs in logits to scheduler_stats if output is corrupted (#18777) 

### Platform & Deployment
* No privileged CPU / Docker / K8s mode (#19241) and custom default max tokens for hosted platforms (#18557).  
* Security hardening  runtime (cloud)pickle imports forbidden (#18018).  
* Hermetic builds and wheel slimming (FA2 8.0 + PTX only) shrink supply chain surface (#18064, #19336).  

## v0.9.1 (2025-06-10)

This release features **274 commits, from 123 contributors (27 new contributors!)**

* Progress in large scale serving
	* DP Attention + Expert Parallelism: CUDA graph support (#18724), DeepEP dispatch-combine kernel (#18434), batched/masked DeepGEMM kernel (#19111), CUTLASS MoE kernel with PPLX (#18762)
	* Heterogeneous TP (#18833), NixlConnector Enable FlashInfer backend (#19090)
	* DP: API-server scaleout with many-to-many server-engine comms (#17546), Support DP with Ray (#18779), allow AsyncLLMEngine.generate to target a specific DP rank (#19102), data parallel rank to KVEventBatch (#18925)
	* Tooling: Simplify EP kernels installation (#19412)
* RLHF workflow: Support inplace model weights loading (#18745)
* Initial full support for Hybrid Memory Allocator (#17996), support cross-layer KV sharing (#18212)
* Add FlexAttention to vLLM V1 (#16078)
* Various production hardening related to full cuda graph mode (#19171, #19106, #19321)

### Model Support
* Support Magistral (#19193), LoRA support for InternVL (#18842), minicpm eagle support (#18943), NemotronH support (#18863, #19249)
* Enable data parallel for Llama4 vision encoder (#18368)
* Add DeepSeek-R1-0528 function call chat template (#18874)

### Hardware Support & Performance Optimizations
* Add H20-3e fused MoE kernel tuning configs for DeepSeek-R1/V3 (#19205), Qwen3-235B-A22B (#19315)
* Blackwell: Add Cutlass MLA backend (#17625), Tunings for SM100 FP8 CUTLASS kernel (#18778), Use FlashInfer by default on Blackwell GPUs (#19118), Tune `scaled_fp8_quant` by increasing vectorization (#18844)
* FP4: Add compressed-tensors NVFP4 support (#18312), FP4 MoE kernel optimization (#19110)
* CPU: V1 support for the CPU backend (#16441)
* ROCm: Add AITER grouped topk for DeepSeekV2 (#18825)
* POWER: Add IBM POWER11 Support to CPU Extension Detection (#19082)
* TPU: Initial support of model parallelism with single worker using SPMD (#18011), Multi-LoRA Optimizations for the V1 TPU backend (#15655)
* Neuron: Add multi-LoRA support for Neuron. (#18284), Add Multi-Modal model support for Neuron (#18921), Support quantization on neuron (#18283)
* Platform: Make torch distributed process group extendable (#18763)

### Engine features
* Add Lora Support to Beam Search (#18346)
* Add rerank support to run_batch endpoint (#16278)
* CLI: add run batch (#18804)
* Server: custom logging (#18403), allowed_token_ids in ChatCompletionRequest (#19143)
* `LLM` API: make use_tqdm accept a callable for custom progress bars (#19357)
* perf: [KERNEL] Sampler. CUDA kernel for applying repetition penalty (#18437)

### API Deprecations
* Disallow pos-args other than `model` when initializing `LLM` (#18802)
* Remove `inputs` arg fallback in Engine classes (#18799)
* Remove fallbacks for Embeddings API (#18795)
* Remove mean pooling default for `Qwen2EmbeddingModel` (#18913)
* Require overriding `get_dummy_text` and `get_dummy_mm_data` (#18796)
* Remove metrics that were deprecated in 0.8 (#18837)

### Documentation
* Add CLI doc (#18871)
* Update SECURITY.md with link to our security guide (#18961), Add security warning to bug report template (#19365)

## v0.9.0 (2025-05-15)

This release features 649 commits, from 215 contributors (82 new contributors!)

* vLLM has upgraded to PyTorch 2.7!  (#16859) This is a breaking change for environment dependency.
	* The default wheel has been upgraded from CUDA 12.4 to CUDA 12.8. We will distribute CUDA 12.6 wheel on GitHub artifact. 
	* As a general rule of thumb, our CUDA version policy follow PyTorch's CUDA version policy. 
* Enhanced NVIDIA Blackwell support. vLLM now ships with initial set of optimized kernels on NVIDIA Blackwell with both attention and mlp. 
	* You can use our docker image or install FlashInfer nightly wheel `pip install https://download.pytorch.org/whl/cu128/flashinfer/flashinfer_python-0.2.5%2Bcu128torch2.7-cp38-abi3-linux_x86_64.whl` then set `VLLM_ATTENTION_BACKEND=FLASHINFER` for better performance.
	* Upgraded support for the new FlashInfer main branch. (#15777)
	* Please checkout https://github.com/vllm-project/vllm/issues/18153 for the full roadmap 
* Initial DP, EP, PD support for large scale inference
	* EP:
		* Permute and unpermute kernel for moe optimization (#14568)
		* Modularize fused experts and integrate PPLX kernels (#15956)
		* Refactor pplx init logic to make it modular (prepare for deepep) (#18200)
		* Add ep group and all2all interface (#18077)
	* DP:
		* Decouple engine process management and comms (#15977)
	* PD:
		* NIXL Integration (#17751)
		* Local attention optimization for NIXL (#18170)
		* Support multiple kv connectors (#17564)
* Migrate docs from Sphinx to MkDocs (#18145, #18610, #18614, #18616. #18622, #18626, #18627, #18635, #18637, #18657, #18663, #18666, #18713)

### Notable Changes
* Removal of CUDA 12.4 support due to PyTorch upgrade to 2.7. 
* Change `top_k` to be disabled with `0` (still accept `-1` for now) (#17773)
* The seed is now set to `0` by default for V1 Engine, meaning that different vLLM runs now yield the same outputs even if `temperature > 0`. This does not modify the random state in user code since workers are run in separate processes unless `VLLM_USE_V1_MULTIPROCESSING=0`. (#17929, #18741)

### Model Enhancements
* Support MiMo-7B (#17433), MiniMax-VL-01 (#16328), Ovis 1.6 (#17861), Ovis 2 (#15826), GraniteMoeHybrid 4.0 (#17497), FalconH1\* (#18406), LlamaGuard4 (#17315)
  * Please install the development version of `transformers` (from source) to use Falcon-H1.
* Embedding models: nomic-embed-text-v2-moe (#17785), new class of gte models (#17986)
* Progress in Hybrid Memory Allocator (#17394, #17479, #17474, #17483, #17193, #17946, #17945, #17999, #18001, #18593)
* DeepSeek: perf enhancement by moving more calls into cuda-graph region(#17484, #17668), Function Call (#17784), MTP in V1 (#18435)
* Qwen2.5-1M: Implements dual-chunk-flash-attn backend for dual chunk attention with sparse attention support (#11844)
* Qwen2.5-VL speed enhancement via rotary_emb optimization (#17973)
* InternVL models with Qwen2.5 backbone now support video inputs (#18499)

### Performance, Production and Scaling
* Support full cuda graph in v1 (#16072)
* Pipeline Parallelism: MultiprocExecutor support (#14219), `torchrun` (#17827)
* Support sequence parallelism combined with pipeline parallelism (#18243)
* Async tensor parallelism using compilation pass (#17882)
* Perf: Use small max_num_batched_tokens for A100 (#17885)
* Fast Model Loading: Tensorizer support for V1 and LoRA (#17926)
* Multi-modality: Automatically cast multi-modal input dtype before transferring device (#18756)

### Security
* Prevent side-channel attacks via cache salting (#17045)
* Fix image hash collision in certain edge cases (#17378)
* Add `VLLM_ALLOW_INSECURE_SERIALIZATION` env var (#17490)
* Migrate to REGEX Library to prevent catastrophic backtracking (#18454, #18750)

### Features
* CLI: `deprecated=True` (#17426)
* Frontend: progress bar for adding requests (#17525), `chat_template_kwargs` in `LLM.chat` (#17356), `/classify` endpoint (#17032), truncation control for embedding models (#14776), `cached_tokens` in response usage (#18149)
* LoRA: default local directory LoRA resolver plugin. (#16855)
* Metrics: kv event publishing (#16750), API for accessing in-memory Prometheus metrics (#17010)
* Quantization: `nvidia/DeepSeek-R1-FP4` (#16362), Quark MXFP4 format (#16943), AutoRound (#17850), torchao models with `AOPerModuleConfig` (#17826), CUDA Graph support for V1 GGUF support (#18646)
* Reasoning: deprecate `--enable-reasoning` (#17452)
* Spec Decode: EAGLE share input embedding (#17326), torch.compile & cudagraph to EAGLE (#17211), EAGLE3 (#17504), log accumulated metrics(#17913), Medusa (#17956)
* Structured Outputs: Thinking compatibility (#16577), Spec Decoding (#14702), Qwen3 reasoning parser (#17466), `tool_choice: required` for Xgrammar (#17845), Structural Tag with Guidance backend (#17333)
* Transformers backend: named parameters (#16868), interleaved sliding window attention (#18494)

### Hardwares
* NVIDIA: cutlass support for blackwell fp8 blockwise gemm (#14383)
* TPU: Multi-LoRA implementation(#14238), default max-num-batched-tokens (#17508), V1 backend by default (#17673), top-logprobs (#17072)
* Neuron: NeuronxDistributedInference support (#15970), Speculative Decoding, Dynamic on-device sampling (#16357), Mistral Model (#18222), Multi-LoRA (#18284)
* AMD: Enable FP8 KV cache on V1 (#17870), Tuned fused moe config for Qwen3 MoE on MI300X (#17535, #17530), AITER biased group topk (#17955), Block-Scaled GEMM (#14968), MLA (#17523), Radeon GPU use Custom Paged Attention (#17004), reduce the number of environment variables in command line (#17229)
* Extensibility: Make PiecewiseBackend pluggable and extendable (#18076)

### Documentation
* Update quickstart and install for cu128 using `--torch-backend=auto` (#18505)
* NVIDIA TensorRT Model Optimizer (#17561)
* Usage of Qwen3 thinking (#18291)

### Developer Facing 
* Benchmark: Add single turn MTBench to Serving Bench (#17202)
* Usability: Decrease import time of `vllm.multimodal` (#18031)
* Code Format: Code formatting using `ruff format` (#17656, #18068, #18400)
* Readability: 
	* Configuration and arguments unification is now complete! (#17130, #17453, #17562)
	* Update deprecated type hinting from Python 3.7 (#18056, #18130, #18132, #18129, #18073, #18072, #18126, #18128, #18057, #18058)
* Process:
	* Propose a deprecation policy for the project (#17063)
* Testing: expanding torch nightly tests (#18004)