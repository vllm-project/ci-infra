"""An edit to one op in a many-op native file routes as that op.

vllm#58828 added one parameter to get_cutlass_moe_mm_data: its prototype in
ops.h, its schema in torch_bindings.cpp, its entry and caller in
scaled_mm_entry.cu, and a device helper in moe_data.cu. Each file owns many
ops, so each routed through all of their tests and wrappers: 261 steps.
"""

from __future__ import annotations

import dataclasses
import subprocess
from pathlib import Path
import pytest
from ci_selector.codemap.classify import _narrowed_native_state
from ci_selector.codemap.native_ops import NativeOps
from ci_selector.codemap.state import DiffContext

OPS = frozenset({"moe_data", "scaled_mm", "other_op"})

HEADER = """\
#pragma once
void moe_data(
    const Tensor& topk_ids,
    const bool is_gated);

void scaled_mm(Tensor& out, const Tensor& a);
"""

BINDINGS = """\
LIBRARY_FRAGMENT(_C, ops) {
  ops.def(
      "moe_data(Tensor topk_ids, "
      "         bool is_gated) -> ()");
  ops.impl("moe_data", kCUDA, &moe_data);
  ops.def("scaled_mm(Tensor! out, Tensor a) -> ()");
}
"""

KERNELS = """\
namespace {

__device__ int map_id(int e, const int* m) {
  return m == nullptr ? e : m[e];
}

__global__ void count_kernel(const int* ids, const int* m) {
  int x = map_id(ids[0], m);
}

void launch_count(const int* ids, const int* m) {
  count_kernel<<<1, 1>>>(ids, m);
}

}  // namespace

void moe_data_caller(const int* ids, const int* m) {
  launch_count(ids, m);
}

void scaled_mm_caller(float* out) {
  out[0] = 0;
}
"""


class Repo:
    def __init__(self, root: Path):
        self.root = root
        self.git("init", "-q")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "t")

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.root), *args],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    def commit(self, files: dict[str, str]) -> str:
        for path, text in files.items():
            full = self.root / path
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(text)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "c")
        return self.git("rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    r = Repo(tmp_path)
    r.commit({"csrc/ops.h": HEADER, "csrc/bindings.cpp": BINDINGS, "csrc/k.cu": KERNELS})
    return r


def _narrow(repo: Repo, path: str, text: str):
    base = repo.git("rev-parse", "HEAD")
    head = repo.commit({path: text})
    # dataclasses.replace needs a dataclass; the real RepoState is one.
    state = dataclasses.make_dataclass("S", ["repo", "native_ops"])(
        repo.root, NativeOps(file_ops={path: OPS})
    )
    narrowed = _narrowed_native_state(state, path, DiffContext(base, head, {path: "M"}))
    return None if narrowed is None else narrowed.native_ops.file_ops[path]


def test_a_new_parameter_in_a_prototype_is_that_op(repo: Repo):
    text = HEADER.replace(
        "const bool is_gated);", "const bool is_gated,\n    const Tensor& expert_map);"
    )
    assert _narrow(repo, "csrc/ops.h", text) == {"moe_data"}


def test_a_schema_edit_inside_the_registration_block_is_that_op(repo: Repo):
    text = BINDINGS.replace("bool is_gated) -> ()", "bool is_gated, Tensor? m=None) -> ()")
    assert _narrow(repo, "csrc/bindings.cpp", text) == {"moe_data"}


def test_a_device_helper_follows_its_callers_to_the_op(repo: Repo):
    """map_id <- count_kernel <- launch_count <- moe_data_caller, inside a
    namespace, which is not a function body."""
    text = KERNELS.replace("return m == nullptr ? e : m[e];", "return m ? m[e] : e;")
    assert _narrow(repo, "csrc/k.cu", text) == {"moe_data"}


@pytest.mark.parametrize(
    "old, new",
    [
        ("#pragma once\n", "#pragma once\n#define FAST 1\n"),  # names no op
        (
            "void scaled_mm(Tensor& out, const Tensor& a);",
            "void scaled_mm(Tensor& out, const Tensor& a);\nvoid helper();",
        ),
    ],
    ids=["macro", "new-declaration"],
)
def test_a_line_no_op_owns_keeps_the_whole_file(repo: Repo, old, new):
    assert _narrow(repo, "csrc/ops.h", HEADER.replace(old, new)) is None


def test_blank_and_comment_lines_change_no_op(repo: Repo):
    text = KERNELS.replace(
        "void scaled_mm_caller(float* out) {\n",
        "// fast path\n\nvoid scaled_mm_caller(float* out) {\n",
    ).replace("out[0] = 0;", "out[0] = 1;")
    assert _narrow(repo, "csrc/k.cu", text) == {"scaled_mm"}
