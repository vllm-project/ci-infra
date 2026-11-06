"""A CMakeLists.txt edit routes as the csrc sources it reaches.

The file is copied into the image, so any edit to it ran everything. Seen on
vLLM in the week to 2026-11-04: a one-line Marlin arch-list change, new
per-dtype source lists, a new kernel's guarded block. Each touched one
kernel's corner of the file.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from ci_selector.codemap import classify
from ci_selector.codemap.claim import Claim
from ci_selector.codemap.cmake_sources import reach
from ci_selector.codemap.selection import Selection
from ci_selector.codemap.state import DiffContext
from ci_selector.decide import _as_cmake_sources, _held_by_cmake

CMAKE = """\
set(VLLM_EXT_SRC
  "csrc/ops.cu"
  "csrc/other.cu")

if(VLLM_GPU_LANG STREQUAL "CUDA")
  # marlin
  cuda_archs_loose_intersection(MARLIN_ARCHS "8.0;12.0f" "${CUDA_ARCHS}")
  if (MARLIN_ARCHS)
    file(GLOB MARLIN_GEN_SRC "csrc/marlin/sm80_kernel_*.cu")
    set_gencode_flags_for_srcs(
      SRCS "${MARLIN_GEN_SRC}"
      CUDA_ARCHS "${MARLIN_ARCHS}")
    list(APPEND VLLM_EXT_SRC ${MARLIN_GEN_SRC})
    set(MARLIN_SRCS
      "csrc/marlin/marlin.cu"
      "csrc/marlin/repack.cu")
    set_gencode_flags_for_srcs(
      SRCS "${MARLIN_SRCS}"
      CUDA_ARCHS "${MARLIN_ARCHS}")
    list(APPEND VLLM_EXT_SRC "${MARLIN_SRCS}")
  endif()
  cuda_archs_loose_intersection(TOPK_ARCHS "9.0" "${CUDA_ARCHS}")
  if(TOPK_ARCHS)
    list(APPEND VLLM_EXT_SRC "csrc/topk.cu")
    target_compile_definitions(_C PRIVATE VLLM_ENABLE_TOPK=1)
  endif()
endif()

if(VLLM_GPU_LANG STREQUAL "HIP")
  set(_WAVE64 ${VLLM_GPU_ARCHES})
  list(FILTER _WAVE64 INCLUDE REGEX "^gfx9")
endif()

define_extension_target(
  _C
  DESTINATION vllm
  SOURCES ${VLLM_EXT_SRC}
  COMPILE_FLAGS ${VLLM_GPU_FLAGS}
  ARCHITECTURES ${VLLM_GPU_ARCHES})
"""

FILES = {
    "csrc/ops.cu": "void ops() {}\n",
    "csrc/other.cu": "void other() {}\n",
    "csrc/topk.cu": "void topk() {}\n",
    "csrc/torch_bindings.cpp": "#ifdef VLLM_ENABLE_TOPK\nvoid reg() {}\n#endif\n",
    "csrc/marlin/marlin.cu": "void marlin() {}\n",
    "csrc/marlin/repack.cu": "void repack() {}\n",
    "csrc/marlin/kernel.h": "#pragma once\n",
    "csrc/marlin/generate_kernels.py": "print()\n",
    "csrc/marlin/.gitignore": "sm80_kernel_*.cu\n",
}


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "r"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    for path, text in {**FILES, "CMakeLists.txt": CMAKE}.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    return root


def _edit(repo: Path, old: str, new: str, *more: str) -> tuple[str, str]:
    """One commit replacing `old` with `new`, and each further pair in `more`."""
    base = _git(repo, "rev-parse", "HEAD")
    text = (repo / "CMakeLists.txt").read_text()
    pairs = [(old, new), *zip(more[::2], more[1::2])]
    for a, b in pairs:
        assert a in text
        text = text.replace(a, b, 1)
    (repo / "CMakeLists.txt").write_text(text)
    _git(repo, "commit", "-q", "-am", "edit")
    return base, _git(repo, "rev-parse", "HEAD")


def test_an_arch_list_reaches_the_sources_it_compiles(repo: Path):
    r = reach(repo, *_edit(repo, '"8.0;12.0f"', '"8.0"'))
    # The generated glob stands for its directory's tracked sources: the
    # headers the generated kernels include, not the generator or .gitignore.
    assert r.sources == {
        "csrc/marlin/kernel.h",
        "csrc/marlin/marlin.cu",
        "csrc/marlin/repack.cu",
    }
    assert not r.why_not and not r.other_families
    assert r.only_removes_arches


def test_an_added_arch_may_compile_more(repo: Path):
    r = reach(repo, *_edit(repo, '"8.0;12.0f"', '"8.0;10.0;12.0f"'))
    assert "csrc/marlin/marlin.cu" in r.sources
    assert not r.only_removes_arches


GUARDED = (
    "  if(${CMAKE_CUDA_COMPILER_VERSION} VERSION_GREATER_EQUAL 13.0)\n"
    '    cuda_archs_loose_intersection(MARLIN_ARCHS "8.0;12.0f" "${CUDA_ARCHS}")\n'
    "  else()\n"
    '    cuda_archs_loose_intersection(MARLIN_ARCHS "8.0;12.0a" "${CUDA_ARCHS}")\n'
    "  endif()\n"
)
FLAT = '  cuda_archs_loose_intersection(MARLIN_ARCHS "8.0;12.0f" "${CUDA_ARCHS}")\n'


@pytest.mark.parametrize(
    "new, removes",
    [
        # vllm#65540: drop the SM12x cubins, and the compiler guard with them.
        ('  cuda_archs_loose_intersection(MARLIN_ARCHS "8.0" "${CUDA_ARCHS}")\n', True),
        # 12.0f was only in one arm: CUDA < 13 now compiles it.
        (FLAT, False),
        # Same arches, but intersected with another list.
        ('  cuda_archs_loose_intersection(MARLIN_ARCHS "8.0" "${SM_ARCHS}")\n', False),
    ],
    ids=["drops-arches-and-guard", "arch-from-one-arm", "other-arguments"],
)
def test_collapsing_a_guarded_arch_list(repo: Path, new: str, removes: bool):
    _edit(repo, FLAT, GUARDED)
    r = reach(repo, *_edit(repo, GUARDED, new))
    assert r.sources and r.only_removes_arches is removes


def test_an_arch_list_moved_out_of_its_guard_may_compile_more(repo: Path):
    """Fewer arches, but now set when ENABLE_MARLIN is off, where it was not."""
    _edit(repo, FLAT, "  if(ENABLE_MARLIN)\n  " + FLAT + "  endif()\n")
    r = reach(
        repo,
        *_edit(
            repo,
            "  if(ENABLE_MARLIN)\n  " + FLAT + "  endif()\n",
            "  if(ENABLE_MARLIN)\n  endif()\n" + FLAT.replace('"8.0;12.0f"', '"8.0"'),
        ),
    )
    assert r.sources and not r.only_removes_arches


def test_dropping_a_guard_around_kept_code_may_compile_more(repo: Path):
    r = reach(
        repo,
        *_edit(
            repo,
            '"8.0;12.0f"',
            '"8.0"',
            '  if(TOPK_ARCHS)\n    list(APPEND VLLM_EXT_SRC "csrc/topk.cu")\n',
            '    list(APPEND VLLM_EXT_SRC "csrc/topk.cu")\n',
            "    target_compile_definitions(_C PRIVATE VLLM_ENABLE_TOPK=1)\n  endif()\n",
            "    target_compile_definitions(_C PRIVATE VLLM_ENABLE_TOPK=1)\n",
        ),
    )
    assert "csrc/topk.cu" in r.sources and not r.only_removes_arches


def test_a_moved_endif_reaches_the_statements_it_moves(repo: Path):
    """Moving the Marlin block's endif() past the topk block nests topk under
    MARLIN_ARCHS: git diffs it as two endif() lines and nothing else."""
    topk = (
        '  cuda_archs_loose_intersection(TOPK_ARCHS "9.0" "${CUDA_ARCHS}")\n'
        "  if(TOPK_ARCHS)\n"
        '    list(APPEND VLLM_EXT_SRC "csrc/topk.cu")\n'
        "    target_compile_definitions(_C PRIVATE VLLM_ENABLE_TOPK=1)\n"
        "  endif()\n"
    )
    base, head = _edit(repo, "  endif()\n" + topk, topk + "  endif()\n")
    diff = _git(repo, "diff", "-U0", base, head)
    assert [line for line in diff.splitlines() if line[:1] in "+-"][2:] == [
        "-  endif()",
        "+  endif()",
    ]
    r = reach(repo, base, head)
    assert not r.inert and not r.why_not
    assert {"csrc/topk.cu", "csrc/marlin/marlin.cu"} <= r.sources
    assert not r.only_removes_arches


def test_an_edit_inside_a_flags_list_is_not_a_source_edit(repo: Path):
    """An inner line of a multi-line list stands for itself only when the list
    names sources. A flags list reaches the extension's COMPILE_FLAGS, which
    declines, even beside an edit the walk can follow."""
    _edit(
        repo,
        "define_extension_target(",
        'list(APPEND VLLM_GPU_FLAGS\n  "-DA=1"\n  "-DB=1")\ndefine_extension_target(',
    )
    r = reach(repo, *_edit(repo, '"-DB=1"', '"-DB=2"', '"8.0;12.0f"', '"8.0"'))
    assert r.why_not and not r.sources


def test_a_line_edited_inside_a_source_list_reaches_that_item(repo: Path):
    r = reach(
        repo, *_edit(repo, '"csrc/marlin/repack.cu")', '"csrc/marlin/repack2.cu")')
    )
    assert r.sources == {"csrc/marlin/repack.cu", "csrc/marlin/repack2.cu"}


def test_an_item_appended_to_an_extension_reaches_that_item(repo: Path):
    r = reach(
        repo, *_edit(repo, '"csrc/other.cu")', '"csrc/other.cu"\n  "csrc/third.cu")')
    )
    assert r.sources == {"csrc/other.cu", "csrc/third.cu"}


def test_a_compile_definition_reaches_the_files_reading_it(repo: Path):
    r = reach(repo, *_edit(repo, '"9.0" "${CUDA_ARCHS}"', '"9.0;10.0" "${CUDA_ARCHS}"'))
    assert r.sources == {"csrc/topk.cu", "csrc/torch_bindings.cpp"}


def test_comments_alone_change_nothing(repo: Path):
    r = reach(repo, *_edit(repo, "  # marlin\n", "  # marlin kernels, see #123\n"))
    assert r.inert and not r.sources


def test_a_block_only_another_family_builds_runs_that_family(repo: Path):
    r = reach(repo, *_edit(repo, '"^gfx9"', '"^gfx9[0-4]"'))
    assert r.other_families == {"amd"} and not r.sources and not r.why_not


@pytest.mark.parametrize(
    "old, new",
    [
        (
            "define_extension_target(",
            'set(CMAKE_CUDA_FLAGS "-O3")\ndefine_extension_target(',
        ),
        (
            "  COMPILE_FLAGS ${VLLM_GPU_FLAGS}\n",
            "  COMPILE_FLAGS ${VLLM_GPU_FLAGS} -g\n",
        ),
        ("  # marlin\n", '  # marlin\n  set(UNUSED_FLAG "1")\n'),
        ('"csrc/other.cu")', '"csrc/other.cu"\n  "third_party/x.cu")'),
    ],
    ids=["global-flag", "extension-argument", "variable-nobody-reads", "outside-csrc"],
)
def test_anything_the_walk_cannot_follow_declines(repo: Path, old: str, new: str):
    r = reach(repo, *_edit(repo, old, new))
    assert r.why_not and not r.sources


# ---- the claim, and the kernel record's view of it -------------------------


def _state(repo: Path):
    return SimpleNamespace(repo=repo)


def test_the_claim_routes_as_its_sources(repo: Path, monkeypatch):
    base, head = _edit(repo, '"8.0;12.0f"', '"8.0"')
    subs = {
        "csrc/marlin/marlin.cu": Claim(
            "fail-open", "m", step_ids={"a", "b"}, droppable_step_ids={"a"}
        ),
        "csrc/marlin/repack.cu": Claim("fail-open", "r", step_ids={"b"}),
        "csrc/marlin/kernel.h": Claim("fail-open", "k", run_all={"vllm_ci"}),
    }
    monkeypatch.setattr(classify, "_classify", lambda state, p, ctx: subs[p])
    monkeypatch.setattr(classify, "_build_map_allowed", lambda state, fams: {"cuda-1"})
    claim = classify._classify_cmake_edit(
        _state(repo), "CMakeLists.txt", DiffContext(base, head, {"CMakeLists.txt": "M"})
    )
    assert claim.rule == "cmake-sources" and not claim.run_all
    # The unclaimed header runs the CUDA build's steps, not everything.
    assert claim.step_ids == {"a", "b", "cuda-1"}
    assert claim.droppable_step_ids == {"a"}
    assert claim.evidence_paths == frozenset(subs)
    assert claim.image_union_exempt, "else every image consumer comes back"


def test_an_edit_that_may_compile_more_holds_every_step(repo: Path, monkeypatch):
    """A step that never launched Marlin may be the one that starts to once
    its arch is added, so the kernel record may clear none of them."""
    base, head = _edit(repo, '"8.0;12.0f"', '"8.0;10.0;12.0f"')
    sub = Claim("fail-open", "m", step_ids={"a", "b"}, droppable_step_ids={"a"})
    monkeypatch.setattr(classify, "_classify", lambda state, p, ctx: sub)
    monkeypatch.setattr(classify, "_build_map_allowed", lambda state, fams: set())
    claim = classify._classify_cmake_edit(
        _state(repo), "CMakeLists.txt", DiffContext(base, head, {"CMakeLists.txt": "M"})
    )
    assert claim.step_ids == {"a", "b"}
    assert not claim.droppable_step_ids
    assert "CMakeLists.txt" in claim.evidence_paths
    selection = Selection(claims=[claim], selected_by_file={"CMakeLists.txt": ["a"]})
    assert _held_by_cmake(selection) == {"a", "b"}
    view, sources = _as_cmake_sources(selection)
    assert view.selected_by_file == {"CMakeLists.txt": ["a"]}
    assert "csrc/marlin/marlin.cu" in sources


def test_an_arch_removal_is_left_to_the_kernel_record():
    claim = Claim(
        "cmake-sources", "d", step_ids={"s1"}, evidence_paths=frozenset({"csrc/a.cu"})
    )
    assert not _held_by_cmake(Selection(claims=[claim]))


def test_an_edit_the_walk_declines_fails_open_as_before(repo: Path):
    base, head = _edit(
        repo, "  COMPILE_FLAGS ${VLLM_GPU_FLAGS}\n", "  COMPILE_FLAGS -g\n"
    )
    ctx = DiffContext(base, head, {"CMakeLists.txt": "M"})
    assert classify._classify_cmake_edit(_state(repo), "CMakeLists.txt", ctx) is None


def test_the_kernel_record_weighs_the_sources_for_the_steps_they_selected():
    selection = Selection(
        claims=[
            Claim(
                "cmake-sources",
                "d",
                step_ids={"s1"},
                evidence_paths=frozenset({"csrc/a.cu"}),
            )
        ],
        selected_by_file={"CMakeLists.txt": ["s1"], "vllm/x.py": ["s2"]},
    )
    view, sources = _as_cmake_sources(selection)
    assert sources == {"csrc/a.cu"}
    assert view.selected_by_file == {"csrc/a.cu": ["s1"], "vllm/x.py": ["s2"]}
    assert selection.selected_by_file["CMakeLists.txt"] == ["s1"], "original untouched"


def test_a_family_only_edit_keeps_the_file_among_the_evidence():
    selection = Selection(
        claims=[
            Claim(
                "cmake-sources",
                "d",
                step_ids={"s1"},
                evidence_paths=frozenset({"csrc/a.cu", "CMakeLists.txt"}),
            )
        ],
        selected_by_file={"CMakeLists.txt": ["s1"]},
    )
    view, sources = _as_cmake_sources(selection)
    assert sources == {"csrc/a.cu"}
    assert view.selected_by_file == {"CMakeLists.txt": ["s1"]}, "no kernel can clear it"


def test_a_negated_guard_does_not_hide_cuda_code(repo: Path):
    """`if(NOT VLLM_GPU_LANG STREQUAL "CUDA") ... else()` builds the else-arm
    for CUDA. Reading the guard as CUDA-only would skip that arm."""
    base, head = _edit(
        repo,
        'if(VLLM_GPU_LANG STREQUAL "HIP")\n  set(_WAVE64',
        'if(NOT VLLM_GPU_LANG STREQUAL "CUDA")\n  message(STATUS "x")\n'
        'else()\n  list(APPEND VLLM_EXT_SRC "csrc/ops.cu")\nendif()\n'
        'if(VLLM_GPU_LANG STREQUAL "HIP")\n  set(_WAVE64',
    )
    r = reach(repo, base, head)
    assert "csrc/ops.cu" in r.sources


def test_a_variable_a_guard_reads_reaches_every_arm(repo: Path):
    base, head = _edit(
        repo,
        '    list(APPEND VLLM_EXT_SRC "${MARLIN_SRCS}")\n  endif()',
        '    list(APPEND VLLM_EXT_SRC "${MARLIN_SRCS}")\n  else()\n'
        '    list(APPEND VLLM_EXT_SRC "csrc/other.cu")\n  endif()',
    )
    # Commit that structure as the base, then edit only the arch list.
    base, head = _edit(repo, '"8.0;12.0f"', '"8.0"')
    assert "csrc/other.cu" in reach(repo, base, head).sources
