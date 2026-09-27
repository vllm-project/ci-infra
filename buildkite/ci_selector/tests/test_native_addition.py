"""New native code routes by the PR's other files instead of failing open.

vllm#58664 added one fused kernel: a new .cu, its line in CMakeLists.txt, a
prototype in ops.h and a def/impl pair in torch_bindings.cpp. Each of those
fell open to "run everything" or to every op in its file.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from ci_selector.codemap.classify import _classify_native_addition
from ci_selector.codemap.state import DiffContext

CMAKE = """\
set(VLLM_EXT_SRC
    "csrc/old.cu"
    "csrc/other.cu"
)
"""
OPS_H = """\
#pragma once
void old_op(torch::Tensor& out);
"""
BINDINGS = """\
TORCH_LIBRARY_EXPAND(TORCH_EXTENSION_NAME, ops) {
  ops.def("old_op(Tensor! out) -> ()");
  ops.impl("old_op", torch::kCUDA, &old_op);
}
"""


class Repo:
    def __init__(self, root: Path):
        self.root = root
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "t")

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.root), *args],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    def write(self, path: str, text: str) -> None:
        full = self.root / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(text)

    def commit(self) -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "c")
        return self.git("rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    (tmp_path / "r").mkdir()
    r = Repo(tmp_path / "r")
    r.write("CMakeLists.txt", CMAKE)
    r.write("csrc/ops.h", OPS_H)
    r.write("csrc/torch_bindings.cpp", BINDINGS)
    r.write("csrc/old.cu", "void old_op(torch::Tensor& out) {}\n")
    r.write("csrc/other.cu", "void other() {}\n")
    r.commit()
    return r


def _claim(repo: Repo, path: str, edits: dict[str, str | None]):
    base = repo.git("rev-parse", "HEAD")
    for p, text in edits.items():
        if text is None:
            (repo.root / p).unlink()
        else:
            repo.write(p, text)
    head = repo.commit()
    status = dict(
        line.split("\t")[::-1]
        for line in repo.git("diff", "--name-status", base, head).splitlines()
    )
    state = SimpleNamespace(
        repo=repo.root,
        native_ops=SimpleNamespace(
            error=None, file_ops={"csrc/old.cu": frozenset({"old_op"})}
        ),
    )
    return _classify_native_addition(state, path, DiffContext(base, head, status))


NEW_CU = {"csrc/new.cu": "void new_op(torch::Tensor& out) {}\n"}


def test_a_new_native_source_claims_nothing_of_its_own(repo: Repo):
    claim = _claim(repo, "csrc/new.cu", NEW_CU)
    assert claim.rule == "added-native-source"
    assert claim.step_ids == set() and not claim.run_all
    assert claim.image_union_exempt, "else every image consumer comes back"


def test_a_cmake_line_adding_a_new_source_routes_as_that_source(repo: Repo):
    cmake = CMAKE.replace('"csrc/other.cu"', '"csrc/other.cu"\n    "csrc/new.cu"')
    claim = _claim(repo, "CMakeLists.txt", {**NEW_CU, "CMakeLists.txt": cmake})
    assert claim.rule == "added-native-source"
    assert claim.evidence_paths == frozenset({"csrc/new.cu"})


@pytest.mark.parametrize(
    "cmake",
    [
        CMAKE + 'set(CMAKE_CUDA_FLAGS "-O3")\n',  # a flag, not a source
        CMAKE.replace('"csrc/other.cu"', '"csrc/other.cu"\n    "csrc/old2.cu"'),
        CMAKE.replace('    "csrc/other.cu"\n', ""),  # a removal
    ],
    ids=["flag", "source-not-new", "removal"],
)
def test_any_other_cmake_change_falls_through(repo: Repo, cmake: str):
    edits = {**NEW_CU, "CMakeLists.txt": cmake}
    if "old2" in cmake:
        edits = {"csrc/old2.cu": "void x() {}\n", "CMakeLists.txt": cmake}
        repo.write("csrc/old2.cu", "void x() {}\n")
        repo.commit()
        edits.pop("csrc/old2.cu")
    assert _claim(repo, "CMakeLists.txt", edits) is None


def test_a_header_prototype_for_a_new_name_claims_nothing(repo: Repo):
    header = OPS_H + "void new_op(\n    torch::Tensor& out,\n    int n);\n"
    claim = _claim(repo, "csrc/ops.h", {**NEW_CU, "csrc/ops.h": header})
    assert claim.rule == "added-native-source" and claim.step_ids == set()


@pytest.mark.parametrize(
    "addition",
    [
        "inline void new_op(torch::Tensor& out) { out.zero_(); }\n",  # a body
        "#define NEW_OP 1\n",  # a macro
        "void old_op(torch::Tensor& out, int n);\n",  # names an existing op
    ],
    ids=["body", "macro", "existing-op"],
)
def test_a_header_edit_with_code_or_an_old_op_falls_through(repo: Repo, addition):
    assert _claim(repo, "csrc/ops.h", {"csrc/ops.h": OPS_H + addition}) is None


def test_registering_a_new_op_claims_nothing(repo: Repo):
    bindings = BINDINGS.replace(
        "}\n",
        '  ops.def("new_op(Tensor! out) -> ()");\n'
        '  ops.impl("new_op", torch::kCUDA, &new_op);\n}\n',
    )
    claim = _claim(
        repo, "csrc/torch_bindings.cpp", {**NEW_CU, "csrc/torch_bindings.cpp": bindings}
    )
    assert claim.rule == "added-native-source" and claim.step_ids == set()


def test_re_registering_an_existing_op_falls_through(repo: Repo):
    bindings = BINDINGS.replace(
        "}\n", '  ops.impl("old_op", torch::kCPU, &old_op_cpu);\n}\n'
    )
    assert (
        _claim(repo, "csrc/torch_bindings.cpp", {"csrc/torch_bindings.cpp": bindings})
        is None
    )


def test_an_edit_to_an_existing_kernel_falls_through(repo: Repo):
    """A hunk inside an existing function never reaches the header or
    registration shapes: a .cu is neither."""
    edits = {"csrc/old.cu": "void old_op(torch::Tensor& out) { int fresh_var = 1; }\n"}
    assert _claim(repo, "csrc/old.cu", edits) is None
