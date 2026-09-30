# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""A version-only bump of a watched library runs the steps that call it.

Two halves: recognising a file whose whole change is a pin moving, where any
other edit must leave the file to the rules that exist today, and the drop
itself, which every doubt resolves toward keeping.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from ci_selector.codemap.selection import Selection
from ci_selector.coverage import libraries
from ci_selector.coverage.libraries import _diff_bumps, _json_bumps, bumps, read_pr
from ci_selector.coverage.rules import RowKeys
from ci_selector.coverage.table import load
from ci_selector.gitdiff import diff_files
from ci_selector.scripts.build import merge_build, write_table

from .helpers import Build, Repo, process_file


def _diff(*pairs: tuple[str, str]) -> str:
    """A -U0 diff, one hunk per (old, new) line pair."""
    out = ["diff --git a/f b/f", "--- a/f", "+++ b/f"]
    for i, (old, new) in enumerate(pairs, 1):
        out += [f"@@ -{i} +{i} @@", f"-{old}", f"+{new}"]
    return "\n".join(out) + "\n"


class TestRecognisingABump:
    @pytest.mark.parametrize(
        "old,new",
        [
            ("flashinfer-python==0.7.0", "flashinfer-python==0.7.1"),
            ("flashinfer-jit-cache==0.7.0+cu134", "flashinfer-jit-cache==0.7.1+cu134"),
            ("ARG FLASHINFER_VERSION=0.7.0", "ARG FLASHINFER_VERSION=0.7.1"),
            ("triton==3.7.1", "triton==3.8.0"),
            (
                '  set(_DEEPGEMM_UPSTREAM_TAG "e1f418c2a4f20818221f6b0e578b4c2f634d4c3f")',
                '  set(_DEEPGEMM_UPSTREAM_TAG "0123456789abcdef0123456789abcdef01234567")',
            ),
        ],
    )
    def test_a_pin_moving(self, old, new):
        assert _diff_bumps(_diff((old, new)))

    @pytest.mark.parametrize(
        "old,new",
        [
            # Not a watched library, or not the one it looks like.
            ("torch==2.13.0", "torch==2.14.0"),
            ("tritonclient==2.64.0", "tritonclient==2.65.0"),
            ("conch-triton-kernels==1.2.1", "conch-triton-kernels==1.2.2"),
            # Something besides the version moved.
            ("flashinfer-python==0.7.0", "flashinfer-python>=0.7.0"),
            ("flashinfer-python==0.7.0", "flashinfer-python[jit]==0.7.1"),
            ("flashinfer-python==0.7.0", "flashinfer-cubin==0.7.1"),
            (
                "--extra-index-url https://flashinfer.ai/whl/cu130",
                "--extra-index-url https://flashinfer.ai/whl/cu134",
            ),
        ],
    )
    def test_anything_else(self, old, new):
        assert _diff_bumps(_diff((old, new))) is None

    def test_one_other_line_takes_the_whole_file(self):
        """Today's rules keep a file with any other change in it."""
        diff = _diff(("flashinfer-python==0.7.0", "flashinfer-python==0.7.1"))
        diff += "@@ -9,0 +10 @@\n+tilelang==0.1.12\n"
        assert _diff_bumps(diff) is None

    def test_a_removed_line_starting_with_dashes_is_a_line(self):
        """`---` opens the file header, and also a removed `--extra-index-url`."""
        diff = _diff(("--extra-index-url https://x/", "--extra-index-url https://y/"))
        assert _diff_bumps(diff) is None

    def test_versions_json_by_key(self):
        old = {"variable": {"FLASHINFER_VERSION": {"default": "0.7.0"}}}
        new = {"variable": {"FLASHINFER_VERSION": {"default": "0.7.1"}}}
        assert _json_bumps(old, new) == {"flashinfer"}

    def test_versions_json_other_keys(self):
        old = {
            "variable": {
                "FLASHINFER_VERSION": {"default": "0.7.0"},
                "X": {"default": "1"},
            }
        }
        new = {
            "variable": {
                "FLASHINFER_VERSION": {"default": "0.7.1"},
                "X": {"default": "2"},
            }
        }
        assert _json_bumps(old, new) is None
        del new["variable"]["X"]
        assert _json_bumps(old, new) is None

    def test_bumps_reads_the_diff(self, tmp_repo: Repo):
        tmp_repo.write(
            "requirements/cuda.txt", "torch==2.13.0\nflashinfer-python==0.7.0\n"
        )
        tmp_repo.write(
            "docker/versions.json", '{"FLASHINFER_VERSION": {"default": "0.7.0"}}\n'
        )
        tmp_repo.write("docker/Dockerfile", "ARG FLASHINFER_VERSION=0.7.0\nRUN echo\n")
        base = tmp_repo.commit("pins")
        tmp_repo.write(
            "requirements/cuda.txt", "torch==2.13.0\nflashinfer-python==0.7.1\n"
        )
        tmp_repo.write(
            "docker/versions.json", '{"FLASHINFER_VERSION": {"default": "0.7.1"}}\n'
        )
        tmp_repo.write(
            "docker/Dockerfile", "ARG FLASHINFER_VERSION=0.7.1\nRUN echo hi\n"
        )
        head = tmp_repo.commit("bump")
        got = bumps(tmp_repo.root, base, head, diff_files(tmp_repo.root, base, head))
        # The Dockerfile changed a command too, so it is not a bump.
        assert got == {
            "requirements/cuda.txt": frozenset({"flashinfer"}),
            "docker/versions.json": frozenset({"flashinfer"}),
        }


class TestTheDrop:
    """One step per case, each selected for `requirements/cuda.txt` by the
    image rule, as the map selects them for a real bump."""

    BUMPED = {"requirements/cuda.txt": frozenset({"flashinfer"})}

    def _table(self, tmp_path: Path, tmp_repo: Repo, **rows):
        """rows: step key -> the `process_file` extras its one process gets."""
        build = Build(tmp_path / "sweep" / "b", "1", tmp_repo.head())
        for i, (key, extra) in enumerate(rows.items()):
            extra = dict(extra)
            process_file(
                build.job(f"j{i}", step_key=key) / "fn.a.txt",
                [("mod.py", "plain"), *extra.pop("entries", ())],
                **{"packages": ["torch"], **extra},
            )
        out = tmp_path / "table.json"
        write_table(merge_build(build.finish(), tmp_repo.root), out)
        return load(out)

    def _selection(self, *keys: str, by=("requirements/cuda.txt",), rule="image-input"):
        sel = Selection()
        for key in keys:
            sid = f"vllm_ci:{key}"
            for path in by:
                sel.selected.setdefault(sid, []).append(f"{path}: image")
                sel.selected_rules.setdefault(sid, []).append(rule)
                sel.selected_paths.setdefault(sid, []).append(None)
                sel.selected_by_file.setdefault(path, []).append(sid)
        return sel

    def _read(self, table, sel, bumped=None, protected=frozenset()):
        keys = RowKeys({"vllm_ci"}, {"vllm_ci": 1.0})
        return read_pr(
            table, sel, self.BUMPED if bumped is None else bumped, keys, protected
        )

    WATCHED = {"libs": ["flashinfer", "triton"]}

    def test_a_step_that_never_called_it_drops(self, tmp_path, tmp_repo):
        table = self._table(tmp_path, tmp_repo, quiet=self.WATCHED)
        dropped, why = self._read(table, self._selection("quiet"))
        assert dropped == ["vllm_ci:quiet"]
        assert why["row-never-calls-the-bumped-library"] == 1

    def test_a_step_that_called_it_keeps(self, tmp_path, tmp_repo):
        calls = {
            **self.WATCHED,
            "libcalls": [("flashinfer", "flashinfer.sampling.top_k")],
        }
        table = self._table(tmp_path, tmp_repo, caller=calls)
        dropped, why = self._read(table, self._selection("caller"))
        assert dropped == [] and why["row-calls-the-bumped-library"] == 1

    def test_a_call_into_another_library_is_not_a_call_into_this_one(
        self, tmp_path, tmp_repo
    ):
        calls = {**self.WATCHED, "libcalls": [("triton", "triton.jit")]}
        table = self._table(tmp_path, tmp_repo, other=calls)
        dropped, _ = self._read(table, self._selection("other"))
        assert dropped == ["vllm_ci:other"]

    def test_a_row_from_before_the_recorder_watched_keeps(self, tmp_path, tmp_repo):
        """Its silence proves nothing: it was never listening."""
        table = self._table(tmp_path, tmp_repo, old={})
        dropped, why = self._read(table, self._selection("old"))
        assert dropped == [] and why["row-predates-library-recording"] == 1

    def test_no_row_keeps(self, tmp_path, tmp_repo):
        table = self._table(tmp_path, tmp_repo, quiet=self.WATCHED)
        dropped, why = self._read(table, self._selection("never-recorded"))
        assert dropped == [] and why["no-row"] == 1

    def test_a_weak_row_keeps(self, tmp_path, tmp_repo):
        weak = {**self.WATCHED, "clean_exit": False}
        table = self._table(tmp_path, tmp_repo, weak=weak)
        dropped, why = self._read(table, self._selection("weak"))
        assert dropped == [] and why["row-too-thin-to-read-a-silence"] == 1

    def test_another_changed_file_keeps_it_for_the_other_rules(
        self, tmp_path, tmp_repo
    ):
        table = self._table(tmp_path, tmp_repo, quiet=self.WATCHED)
        sel = self._selection(
            "quiet", by=("requirements/cuda.txt", "docker/Dockerfile")
        )
        dropped, why = self._read(table, sel)
        assert dropped == [] and not why, "not this rule's to weigh"

    def test_a_step_selected_without_a_file_keeps(self, tmp_path, tmp_repo):
        table = self._table(tmp_path, tmp_repo, quiet=self.WATCHED)
        sel = self._selection("quiet")
        sel.selected_rules["vllm_ci:quiet"].append("always-run")
        dropped, why = self._read(table, sel)
        assert dropped == [] and why["selected-without-a-file"] == 1

    def test_a_step_the_python_record_saw_run_changed_code_keeps(
        self, tmp_path, tmp_repo
    ):
        table = self._table(tmp_path, tmp_repo, quiet=self.WATCHED)
        dropped, why = self._read(
            table, self._selection("quiet"), protected=frozenset({"vllm_ci:quiet"})
        )
        assert dropped == [] and why["held-by-the-python-record"] == 1

    def test_no_bump_weighs_nothing(self, tmp_path, tmp_repo):
        table = self._table(tmp_path, tmp_repo, quiet=self.WATCHED)
        assert self._read(table, self._selection("quiet"), bumped={}) == ([], {})

    DG = {"cmake/external_projects/deepgemm.cmake": frozenset({"deep_gemm"})}
    DG_WATCHED = {"libs": ["deep_gemm"]}
    DG_WRAPPER = {"deep_gemm": libraries.LIBRARY_WRAPPERS["deep_gemm"]}

    def _read_dg(self, table, key, wrappers):
        sel = self._selection(key, by=tuple(self.DG))
        keys = RowKeys({"vllm_ci"}, {"vllm_ci": 1.0})
        return read_pr(table, sel, self.DG, keys, frozenset(), wrappers)

    def test_a_kernel_through_the_wrapper_is_a_call(self, tmp_path, tmp_repo):
        """DeepGEMM's entry points are compiled: build 91980's DeepGEMM step
        recorded no call into it, only vLLM's wrapper running fp8_gemm_nt."""
        tmp_repo.write("vllm/utils/deep_gemm.py", "def fp8_gemm_nt():\n    pass\n")
        tmp_repo.commit("wrapper")
        row = {**self.DG_WATCHED, "entries": [("utils/deep_gemm.py", "fp8_gemm_nt")]}
        table = self._table(tmp_path, tmp_repo, gemm=row)
        dropped, why = self._read_dg(table, "gemm", self.DG_WRAPPER)
        assert dropped == [] and why["row-calls-the-bumped-library"] == 1

    def test_an_availability_check_is_not_a_call(self, tmp_path, tmp_repo):
        checks = [
            ("utils/deep_gemm.py", "is_deep_gemm_supported"),
            ("utils/deep_gemm.py", "DeepGemmQuantScaleFMT.init_oracle_cache"),
        ]
        tmp_repo.write(
            "vllm/utils/deep_gemm.py",
            "class DeepGemmQuantScaleFMT:\n    def init_oracle_cache(self):\n        pass\n"
            "\n\ndef is_deep_gemm_supported():\n    pass\n",
        )
        tmp_repo.commit("wrapper")
        row = {**self.DG_WATCHED, "entries": checks}
        table = self._table(tmp_path, tmp_repo, check=row)
        dropped, _ = self._read_dg(table, "check", self.DG_WRAPPER)
        assert dropped == ["vllm_ci:check"]

    def test_a_moved_wrapper_keeps_every_step(self, tmp_path, tmp_repo):
        """Without the wrapper at the base, a silence may just be a rename."""
        table = self._table(tmp_path, tmp_repo, quiet=self.DG_WATCHED)
        dropped, why = self._read_dg(table, "quiet", {})
        assert dropped == [] and why["library-wrapper-moved"] == 1


def test_the_wrapper_is_found_only_where_it_exists(tmp_repo: Repo):
    tmp_repo.write("vllm/other.py", "x = 1\n")
    without = tmp_repo.commit("no wrapper")
    tmp_repo.write("vllm/utils/deep_gemm.py", "def fp8_gemm_nt():\n    pass\n")
    with_it = tmp_repo.commit("wrapper")
    assert "deep_gemm" not in libraries.wrappers_at(tmp_repo.root, without)
    assert "deep_gemm" in libraries.wrappers_at(tmp_repo.root, with_it)


def test_torch_is_not_watched():
    """A torch bump runs everything; no row of calls may narrow it."""
    assert "torch" not in libraries.LIBRARY_PINS
    assert not libraries._named("torch==2.14.0")


class TestDecide:
    """Wired last in `decide`, after both other records, and only subtracting."""

    def test_a_bump_drops_the_step_that_never_called_the_library(
        self, tmp_path, tmp_repo
    ):
        from types import SimpleNamespace

        from ci_selector.decide import decide

        tmp_repo.write("requirements/cuda.txt", "flashinfer-python==0.7.0\n")
        base = tmp_repo.commit("pin")
        tmp_repo.write("requirements/cuda.txt", "flashinfer-python==0.7.1\n")
        head = tmp_repo.commit("bump")

        watched = ["flashinfer"]
        table = TestTheDrop()._table(
            tmp_path,
            tmp_repo,
            caller={"libs": watched, "libcalls": [("flashinfer", "flashinfer.mm")]},
            quiet={"libs": watched},
        )
        sel = TestTheDrop()._selection("caller", "quiet")
        steps = [
            SimpleNamespace(step_id=f"vllm_ci:{k}", buildkite_key=k, label=k)
            for k in ("caller", "quiet")
        ]
        state = SimpleNamespace(
            pipelines=[
                SimpleNamespace(config=SimpleNamespace(name="vllm_ci"), steps=steps)
            ]
        )
        d = decide(
            state,
            sel,
            tmp_repo.root,
            base,
            head,
            table=table,
            kernels=SimpleNamespace(unavailable="not under test"),
        )
        assert d.bumped == {"requirements/cuda.txt": frozenset({"flashinfer"})}
        assert d.dropped_by_libraries == {"vllm_ci:quiet"}
        assert d.steps == {"vllm_ci:caller"}
