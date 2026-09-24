# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Per-kernel attribution of a CUDA diff.

    changed line in a __global__ body      -> that kernel
    in a __device__ helper                 -> the kernels reaching it
    in a host function                     -> the kernels it launches, through
                                              dispatch helpers
    in a namespace-scope constant or macro -> through the functions using it
    comment, blank, include                -> nothing
    anything else                          -> the whole file

Synthetic source here; the real csrc tree is walked by the drift test in
`tests/test_changed_kernels_drift.py`.
"""

from __future__ import annotations

import difflib

from ci_selector.coverage.changed_kernels import (
    DEVICE,
    HOST,
    KERNEL,
    attribute_text,
    changed_line_numbers,
    host_reach,
    kernel_names_in,
    kernels_reached_by,
    parse,
    symbols_for,
)

SRC = """\
// a comment with braces { and __global__ in it
#include <cuda_runtime.h>
namespace vllm {
namespace {
constexpr int kBlock = 256;
#define TILE 32
}  // namespace

template <typename T>
__device__ __forceinline__ T helper(T x) {
  return x * T(kBlock);
}

__device__ int unused_helper(int x) { return x + 1; }

template <typename T, int N>
__global__ void __launch_bounds__(256) kernel_a(const T* in, T* out) {
  if constexpr (N > 1) {
    out[0] = helper(in[0]);
  }
  const char* s = "}{ not a brace";
  (void)s;
}

__global__ void kernel_b(float* p) {
  p[0] = TILE;
}

template <typename T>
void dispatch_a(const T* in, T* out, cudaStream_t s) {
  kernel_a<T, 2><<<1, kBlock, 0, s>>>(in, out);
}

void launch_a(const float* in, float* out, cudaStream_t s) {
  dispatch_a<float>(in, out, s);
}

void launch_b_ex(float* p) {
  cudaLaunchConfig_t cfg = {};
  cudaLaunchKernelEx(&cfg, kernel_b, p);
}

void host_only(int x) {
  x += 1;
}
}  // namespace vllm
"""

SYMBOLS = frozenset(
    {
        "_ZN4vllm8kernel_aIfLi2EEEvPKT_PS1_",
        "_ZN4vllm8kernel_aI13__nv_bfloat16Li2EEEvPKT_PS1_",
        "_ZN4vllm8kernel_bEPf",
    }
)


def _line(text: str, needle: str) -> int:
    return next(i for i, line in enumerate(text.splitlines(), 1) if needle in line)


def _edit(text: str, needle: str, replacement: str) -> str:
    lines = text.splitlines(keepends=True)
    i = _line(text, needle) - 1
    lines[i] = lines[i].replace(needle, replacement)
    return "".join(lines)


def _diff(a: str, b: str) -> str:
    return "".join(
        difflib.unified_diff(a.splitlines(True), b.splitlines(True), "a", "b", n=0)
    )


def _attr(edited: str, symbols=SYMBOLS):
    return attribute_text(
        "csrc/x.cu", SRC, edited, _diff(SRC, edited), kernel_names_in(symbols)
    )


def test_parse_finds_every_definition_with_kind_and_span():
    ents = {e.name: e for e in parse(SRC)}
    assert ents["helper"].kind == DEVICE and ents["unused_helper"].kind == DEVICE
    assert ents["kernel_a"].kind == KERNEL and ents["kernel_b"].kind == KERNEL
    assert {
        ents[n].kind for n in ("dispatch_a", "launch_a", "launch_b_ex", "host_only")
    } == {HOST}
    # the span starts at the template line and ends at the closing brace
    assert ents["kernel_a"].start == _line(SRC, "template <typename T, int N>")
    assert ents["kernel_a"].end == _line(SRC, "  (void)s;") + 1
    # statements and control flow are not functions; braces in comments and
    # strings do not count
    assert "constexpr" not in ents and "if" not in ents and "namespace" not in ents
    assert ents["dispatch_a"].launches == {"kernel_a"}
    assert ents["launch_b_ex"].launches == {"kernel_b"}


def test_reach_through_helpers_and_dispatchers():
    ents = parse(SRC)
    reached = kernels_reached_by(ents)
    assert reached["helper"] == {"kernel_a"} and reached["unused_helper"] == frozenset()
    hr = host_reach(ents, {"kernel_a", "kernel_b"})
    assert hr["launch_a"] == {"kernel_a"}, "through dispatch_a"
    assert hr["launch_b_ex"] == {"kernel_b"} and hr["host_only"] == frozenset()


def test_changed_line_numbers_cover_insertions_and_deletions():
    old, new = changed_line_numbers("@@ -10,0 +11,3 @@\n@@ -5,2 +4,0 @@\n@@ -7 +7 @@\n")
    assert old == {5, 6, 7} and new == {11, 12, 13, 7}


def test_change_inside_a_kernel_names_that_kernel():
    a = _attr(_edit(SRC, "out[0] = helper(in[0]);", "out[0] = helper(in[0]) + 1;"))
    assert a.narrowed and a.kernels == {"kernel_a"}


def test_change_in_a_helper_names_the_kernels_reaching_it():
    a = _attr(_edit(SRC, "return x * T(kBlock);", "return x * T(kBlock) * 2;"))
    assert a.kernels == {"kernel_a"}
    a = _attr(_edit(SRC, "{ return x + 1; }", "{ return x + 2; }"))
    assert a.file_level and "reaches no kernel" in a.why


def test_change_in_host_code_names_what_it_launches():
    a = _attr(
        _edit(
            SRC,
            "dispatch_a<float>(in, out, s);",
            "dispatch_a<float>(in, out, s);  // pdl",
        )
    )
    # a comment-only edit is inert; make it a real one
    a = _attr(
        _edit(
            SRC,
            "dispatch_a<float>(in, out, s);",
            "dispatch_a<float>(in, out, nullptr);",
        )
    )
    assert a.kernels == {"kernel_a"}, "launch_a -> dispatch_a -> kernel_a"
    a = _attr(
        _edit(
            SRC,
            "cudaLaunchKernelEx(&cfg, kernel_b, p);",
            "cudaLaunchKernelEx(&cfg, kernel_b, p + 1);",
        )
    )
    assert a.kernels == {"kernel_b"}
    a = _attr(_edit(SRC, "x += 1;", "x += 2;"))
    assert a.file_level and "host_only" in a.why


def test_namespace_scope_declarations_go_through_their_users():
    a = _attr(_edit(SRC, "constexpr int kBlock = 256;", "constexpr int kBlock = 128;"))
    assert a.kernels == {"kernel_a"}, "used by helper and by dispatch_a"
    a = _attr(_edit(SRC, "#define TILE 32", "#define TILE 64"))
    assert a.kernels == {"kernel_b"}
    a = _attr(
        SRC.replace(
            "}  // namespace\n", "constexpr int kUnused = 1;\n}  // namespace\n", 1
        )
    )
    assert a.file_level and "outside every function" in a.why


def test_inert_changes_leave_the_file_whole():
    a = _attr(
        _edit(SRC, "// a comment with braces", "// a different comment with braces")
    )
    assert a.file_level and "only comments" in a.why
    a = _attr(
        SRC.replace(
            "#include <cuda_runtime.h>\n",
            "#include <cuda_runtime.h>\n#include <cstdint>\n",
        )
    )
    assert a.file_level and "only comments" in a.why


def test_one_unreadable_side_or_empty_diff_stays_whole():
    a = attribute_text("csrc/x.cu", None, SRC, "@@ -1 +1 @@\n")
    assert a.file_level and "could not be read" in a.why
    assert attribute_text("csrc/x.cu", SRC, SRC, "").file_level


def test_symbols_for_matches_the_mangled_name_component_only():
    syms = frozenset({"_Z3fooIiEvv", "_Z13foobarbaz_kernelv", "foo", "_ZN2ns3fooEv"})
    assert symbols_for(frozenset({"foo"}), syms) == {
        "_Z3fooIiEvv",
        "foo",
        "_ZN2ns3fooEv",
    }
    assert symbols_for(frozenset(), syms) == frozenset()


def test_kernel_names_in_reads_the_function_name_off_the_mangling():
    names = kernel_names_in(
        frozenset(
            {
                "_ZN12tensorrt_llm7kernels21fusedQKNormRopeKernelIN3c104HalfENS2_8BFloat16ELi128ELb0EEEvPviiifPKvS7_S7_PKlii",
                "_Z30concat_and_cache_ds_mla_kernelI13__nv_bfloat16Li64EEvPKT_S3_PvPKliff",
                "_ZN3cub18CUB_300001_SM_10006detail11EmptyKernelIvEEvv",
                "plain_extern_c_kernel",
            }
        )
    )
    assert names == {
        "fusedQKNormRopeKernel",
        "concat_and_cache_ds_mla_kernel",
        "EmptyKernel",
        "plain_extern_c_kernel",
    }


def test_host_launcher_may_name_a_kernel_defined_in_a_header():
    """A .cu holding only host code launches kernels a .cuh defines; the
    map's symbols for the file supply the names."""
    src = "void launch(float* p) {\n  header_kernel<float><<<1, 1>>>(p);\n}\n"
    edited = src.replace("<<<1, 1>>>", "<<<2, 1>>>")
    a = attribute_text(
        "csrc/l.cu", src, edited, _diff(src, edited), frozenset({"header_kernel"})
    )
    assert a.kernels == {"header_kernel"}
