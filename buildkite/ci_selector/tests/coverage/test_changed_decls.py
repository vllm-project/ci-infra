# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Declaration and registration diffs, read as the kernels behind them.

    changed prototype             -> the function it declares
    changed ops.def("op(...)")    -> the function bound to op
    changed ops.impl("op", &fn)   -> fn
    comment, blank, include       -> nothing
    anything else                 -> the whole file

Synthetic source, shaped like vLLM's `csrc/ops.h`, `csrc/torch_bindings.cpp`
and `csrc/cache_kernels.cu` (vllm#42755 is the case this was written for).
"""

from __future__ import annotations

import difflib

from ci_selector.coverage.changed_decls import (
    bindings_of,
    declared_text,
    kernels_behind,
)

OPS_H = """\
#pragma once
#include <torch/all.h>

// Cache ops.
void reshape_and_cache(torch::Tensor& key, torch::Tensor& value,
                       torch::Tensor& v_scale);

void swap_blocks(torch::Tensor& src, torch::Tensor& dst);

inline int helper(int x) { return x + 1; }

constexpr int kTile = 16;
"""

BINDINGS = """\
#include "ops.h"

STABLE_TORCH_LIBRARY_FRAGMENT(_C_cache_ops, ops) {
  // Reshape the key and value tensors and cache them.
  ops.def(
      "reshape_and_cache(Tensor key, Tensor value,"
      "                  Tensor v_scale) -> ()");

  ops.def("swap_blocks(Tensor src, Tensor! dst) -> ()");
}

STABLE_TORCH_LIBRARY_IMPL(_C_cache_ops, CUDA, ops) {
  ops.impl("reshape_and_cache", TORCH_BOX(&reshape_and_cache));
  ops.impl("swap_blocks", TORCH_BOX(&swap_blocks));
  int unused = 0;
}
"""

CACHE_CU = """\
template <typename T>
__global__ void reshape_and_cache_kernel(T* k, bool interleaved_v) {
  k[0] = T(1);
}

#define CALL_RESHAPE_AND_CACHE(T) \\
  reshape_and_cache_kernel<T><<<1, 1, 0, 0>>>(nullptr, interleaved_v);

void reshape_and_cache(torch::Tensor& key, torch::Tensor& value,
                       torch::Tensor& v_scale, bool interleaved_v) {
  DISPATCH(key.dtype(), CALL_RESHAPE_AND_CACHE);
}

void unrelated_host(int x) {
  x += 1;
}
"""


def _diff(a: str, b: str) -> str:
    return "".join(
        difflib.unified_diff(a.splitlines(True), b.splitlines(True), "a", "b", n=0)
    )


def _declared(before: str, after: str):
    return declared_text(before, after, _diff(before, after))


def test_a_prototype_change_names_the_function():
    after = OPS_H.replace(
        "torch::Tensor& v_scale);", "torch::Tensor& v_scale, bool interleaved_v);"
    )
    d = _declared(OPS_H, after)
    assert d.readable and d.functions == {"reshape_and_cache"} and not d.ops


def test_a_schema_change_names_the_op_and_an_impl_change_the_function():
    after = BINDINGS.replace(
        '"                  Tensor v_scale) -> ()");',
        '"                  Tensor v_scale, bool interleaved_v=False) -> ()");',
    )
    d = _declared(BINDINGS, after)
    assert d.readable and d.ops == {"reshape_and_cache"} and not d.functions

    after = BINDINGS.replace("TORCH_BOX(&swap_blocks)", "TORCH_BOX(&swap_blocks_v2)")
    d = _declared(BINDINGS, after)
    assert d.readable and d.functions == {"swap_blocks", "swap_blocks_v2"}


def test_comments_alone_name_nothing():
    after = OPS_H.replace("// Cache ops.", "// Cache ops, all of them.")
    d = _declared(OPS_H, after)
    assert not d.readable and "only comments" in d.why


def test_anything_else_is_the_whole_file():
    for before, after in (
        (OPS_H, OPS_H.replace("return x + 1;", "return x + 2;")),  # inline body
        (OPS_H, OPS_H.replace("kTile = 16", "kTile = 32")),  # a constant
        (BINDINGS, BINDINGS.replace("int unused = 0;", "int unused = 1;")),
    ):
        d = _declared(before, after)
        assert not d.readable and d.why, after


def test_bindings_resolve_an_op_to_its_functions():
    assert bindings_of({"reshape_and_cache"}, [BINDINGS]) == {
        "reshape_and_cache": {"reshape_and_cache"}
    }
    # a commented-out binding does not count
    commented = BINDINGS.replace(
        '  ops.impl("swap_blocks"', '  // ops.impl("swap_blocks"'
    )
    assert bindings_of({"swap_blocks"}, [commented]) == {}


def test_kernels_behind_follow_the_host_launcher():
    def definitions(name):
        return {"csrc/cache_kernels.cu": CACHE_CU} if name in CACHE_CU else {}

    kernels, why = kernels_behind(
        {"reshape_and_cache"}, definitions, lambda path: frozenset()
    )
    assert kernels == {"reshape_and_cache_kernel"} and not why

    # a host function that launches nothing, or a name defined nowhere: the
    # caller keeps the whole file
    for name in ("unrelated_host", "never_defined"):
        kernels, why = kernels_behind({name}, definitions, lambda path: frozenset())
        assert kernels == frozenset() and name in why
