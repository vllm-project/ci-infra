# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""The CUDA parser against the real csrc tree.

A text parser over a tree it does not control; these fail when vLLM's CUDA
starts being written in a shape the parser cannot read, which would send
kernel files back to the whole-file reading without any other signal.
"""

from __future__ import annotations

import pytest
from ci_selector.coverage.changed_kernels import (
    KERNEL,
    host_reach,
    kernels_reached_by,
    parse,
)

pytestmark = pytest.mark.drift


def test_every_cu_parses_and_the_tree_holds_kernels(vllm_repo):
    files = sorted((vllm_repo / "csrc").rglob("*.cu"))
    assert len(files) > 50, "csrc moved?"
    kernels = 0
    for f in files:
        ents = parse(f.read_text(errors="replace"))
        kernels += sum(1 for e in ents if e.kind == KERNEL)
    assert kernels > 150, (
        f"only {kernels} __global__ definitions found across {len(files)} files"
    )


@pytest.mark.parametrize(
    ("path", "kernel", "launcher"),
    [
        (
            "csrc/libtorch_stable/cache_kernels.cu",
            "concat_and_cache_ds_mla_kernel",
            "concat_and_cache_mla",
        ),
        (
            "csrc/libtorch_stable/fused_qknorm_rope_kernel.cu",
            "fusedQKNormRopeKernel",
            "launchFusedQKNormRope",
        ),
        (
            "csrc/libtorch_stable/moe/moe_align_sum_kernels.cu",
            "batched_moe_align_block_size_kernel",
            "batched_moe_align_block_size",
        ),
    ],
)
def test_known_kernels_and_their_launchers_are_read(vllm_repo, path, kernel, launcher):
    ents = parse((vllm_repo / path).read_text(errors="replace"))
    names = {e.name: e for e in ents}
    assert kernel in names and names[kernel].kind == KERNEL, (
        f"{kernel} not parsed as a kernel in {path}"
    )
    assert launcher in names, f"{launcher} not found in {path}"
    reach = host_reach(ents, {e.name for e in ents if e.kind == KERNEL})
    assert kernel in reach[launcher], (
        f"{launcher} does not reach {kernel}: {sorted(reach[launcher])}"
    )
    kernels_reached_by(ents)  # must not raise
