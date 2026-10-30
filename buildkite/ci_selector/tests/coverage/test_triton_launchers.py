# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""A changed Triton kernel is read as the Python that launches it.

A `@triton.jit` body runs on the GPU and never as Python, so no row records
it, and an unknown name holds every step that imports the file. vllm#60502
rewrote 39 kernels and held 138 steps that only imported the kernels' files.
"""

from pathlib import Path

from ci_selector.coverage.changed_funcs import (
    _via_triton_launchers,
    build,
    triton_launchers,
)

from .helpers import Repo

SRC = '''
import triton
import triton.language as tl
from vllm.triton_utils import tl, triton


@triton.jit
def device_fn(x):
    return x


@triton.autotune(configs=[], key=["D"])
@triton.jit(do_not_specialize=["NB"])
def k_main(x, y):
    device_fn(x)


@triton.jit
def k_unlaunched(x):
    pass


@triton.heuristics({"A": lambda a: 1})
@triton.jit
def k_method(x):
    pass


@triton.jit
def k_alias_a(x):
    pass


@triton.jit
def k_alias_b(x):
    pass


def launch(x):
    def grid(meta):
        return (1,)

    k_main[grid](x, x)


def pick(x, big):
    fn = k_alias_a if big else k_alias_b
    fn[(1,)](x)


class Op:
    def forward(self, x):
        def go():
            k_method[(1,)](x)

        go()


def not_a_kernel(x):
    return x
'''


def test_kernels_map_to_their_launchers():
    got = triton_launchers(SRC)
    assert got["k_main"] == {"launch"}
    assert got["device_fn"] == {"launch"}  # only called from inside k_main
    assert got["k_method"] == {"Op.forward", "Op.forward.<locals>.go"}
    assert got["k_alias_a"] == got["k_alias_b"] == {"pick"}
    assert "k_unlaunched" not in got  # nothing in the file launches it
    assert "not_a_kernel" not in got


def test_changed_names_swap_kernels_for_launchers():
    names = frozenset({"k_main", "k_unlaunched", "not_a_kernel"})
    assert _via_triton_launchers(SRC, names) == {"launch", "k_unlaunched", "not_a_kernel"}


def test_files_without_kernels_are_untouched():
    names = frozenset({"f", "g"})
    assert _via_triton_launchers("def f(): pass\ndef g(): pass\n", names) == names
    assert _via_triton_launchers("this is not python (", names) == names
    assert _via_triton_launchers(None, names) == names


def test_a_kernel_body_edit_reads_as_its_launcher(tmp_path: Path):
    """The diff touches only the kernel body; the query names the launcher,
    which is what a row can hold."""
    root = tmp_path / "r"
    root.mkdir()
    repo = Repo(root)
    repo.write("vllm/ops.py", SRC)
    base = repo.commit("base")
    repo.write("vllm/ops.py", SRC.replace("    device_fn(x)\n", "    device_fn(x)\n    device_fn(y)\n"))
    head = repo.commit("edit the kernel body")
    (only,) = build(repo.root, base, head).files
    assert only.names == {"launch"}
    # Stand-ins for the kernel: they may keep a step, never add one.
    assert only.launcher_names == {"launch"}
