#!/usr/bin/env bash
# The kernel-recorder spike: five checks on one CUDA box.
#
#   VLLM_DIR=/path/to/vllm ./spike.sh [outdir]
#
# Needs: a vLLM install that can run its own tests (the CI image is ideal),
# the vLLM checkout for tests/, gcc, and HF access for the fusion e2e model.
# Checks B and F need 1 GPU, E needs 2. Each check prints PASS/FAIL and a
# sample of recorded names; the tally is at the end. Nothing here is fatal:
# a failing check is the information we came for.
#
#   A  direct kernel test        -> fusedQKNormRopeKernel recorded
#   B  fusion e2e, CUDA graphs   -> same kernel recorded from graph replays
#   C  Triton MoE                -> fused_moe_kernel recorded (Triton JIT)
#   D  DeepGEMM                  -> deep_gemm kernels recorded (runtime nvcc JIT)
#   E  TP=2 pynccl               -> two process files, no wrapper needed
#   F  overhead                  -> same test with and without the recorder
#   G  torch.profiler conflict   -> does the recorder survive Kineto?
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
VLLM_DIR="${VLLM_DIR:?set VLLM_DIR to a vLLM checkout containing tests/}"
OUT="${1:-/tmp/kernrec-spike}"
LIB="$HERE/libkernrec.so"
PYTEST="${PYTEST:-pytest -q -x -p no:cacheprovider}"
mkdir -p "$OUT"
PASS=0; FAIL=0

[ -f "$LIB" ] || "$HERE/build.sh"

# run NAME "EXPECT[,EXPECT...]" MIN_PROCS -- pytest args...
run() {
  local name=$1 expects=$2 minprocs=$3; shift 3; [ "$1" = "--" ] && shift
  local dir="$OUT/$name"; rm -rf "$dir"; mkdir -p "$dir"
  echo; echo "=== $name: $PYTEST $*"
  local t0=$SECONDS
  ( cd "$VLLM_DIR" && CUDA_INJECTION64_PATH="$LIB" KERNREC_DIR="$dir" $PYTEST "$@" ) >"$dir/pytest.log" 2>&1
  local rc=$? dt=$((SECONDS - t0))
  local args=()
  IFS=',' read -ra ex <<<"$expects"
  for e in "${ex[@]}"; do [ -n "$e" ] && args+=(--expect "$e"); done
  if python3 "$HERE/summarize.py" "$dir" "${args[@]}" --min-procs "$minprocs" \
       --label "$name" --pytest-rc "$rc" --seconds "$dt" --json "$dir/summary.json"; then
    PASS=$((PASS + 1)); else FAIL=$((FAIL + 1)); fi
  grep -h "kernrec:" "$dir/pytest.log" | sort | uniq -c | head -5 || true
  echo "    log: $dir/pytest.log"
}

# A: the kernel PR #55755 changed, launched directly by its unit test.
run A_direct_kernel "fusedQKNormRopeKernel" 1 -- \
  tests/kernels/core/test_fused_qk_norm_rope.py

# B: the same kernel reached through the torch.compile fusion pass in an
# end-to-end run with CUDA graphs. This is the check that matters most.
run B_fusion_e2e_graphs "fusedQKNormRopeKernel" 1 -- \
  tests/compile/fusions_e2e/test_tp1_quant.py -k test_tp1_fp8_fusions --maxfail=1

# C: Triton. The kernel is a @triton.jit Python function, compiled at first
# call; CUPTI sees it under its Python name.
run C_triton_moe "fused_moe_kernel" 1 -- \
  tests/kernels/moe/test_moe.py -k test_fused_moe

# D: DeepGEMM, nvcc-compiled at runtime inside the deep_gemm package.
run D_deepgemm "deep_gemm,fp8_gemm" 1 -- \
  tests/kernels/moe/test_deepgemm.py -k test_deepgemm_vs_triton --maxfail=1

# E: two ranks, two processes, no wrapper. NCCL kernels should show in both.
run E_tp2_pynccl "" 2 -- \
  tests/distributed/test_pynccl.py

# F: overhead. Same test twice, recorder off then on.
echo; echo "=== F_overhead: baseline (recorder off)"
t0=$SECONDS
( cd "$VLLM_DIR" && $PYTEST tests/kernels/core/test_activation.py ) >"$OUT/F_baseline.log" 2>&1
base=$((SECONDS - t0)); echo "    baseline: ${base}s (rc=$?)"
run F_overhead_recorder "" 1 -- tests/kernels/core/test_activation.py
rec=$(python3 -c "import json;print(json.load(open('$OUT/F_overhead_recorder/summary.json'))['seconds'])")
echo "    overhead: baseline ${base}s -> recorder ${rec}s"

# G: torch.profiler in the same process. Expect pytest to pass either way;
# the question is whether names recorded after the profiler ran still appear.
run G_profiler_conflict "" 1 -- \
  tests/v1/worker/test_gpu_profiler.py --maxfail=1

echo; echo "================ $PASS passed, $FAIL failed  (out: $OUT)"
