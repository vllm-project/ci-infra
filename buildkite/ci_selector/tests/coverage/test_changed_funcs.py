# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""What the diff-to-query contract has to hold to.

The tests that matter here are the ones about precision in one direction and
fail-open in the other. Attributing a body-only edit to `<module>` would be safe
and useless, since every step that imports the file records `<module>`; failing
to attribute it at all would drop steps that run it. Both are checked.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from ci_selector.coverage.changed_funcs import Attribution, attribute, build, names_of

from .helpers import Repo

SAMPLE = '''\
"""Docstring."""

CONSTANT = 1


def plain(argument=CONSTANT):
    inner = argument + 1
    return inner


class Holder:
    field: int = 2

    def method(self):
        return self.field

    class Nested:
        def deep(self):
            return 3


def outer():
    def closure():
        return 4

    return closure


squares = [x for x in range(3)]
gen = (y for y in range(3))
fn = lambda z: z
'''


def line_of(source: str, needle: str) -> int:
    for i, line in enumerate(source.splitlines(), start=1):
        if needle in line:
            return i
    raise AssertionError(f"{needle!r} not in source")


def names_for(source: str, needle: str) -> frozenset[str]:
    names, residue = attribute(source, "sample.py", {line_of(source, needle)})
    assert not residue
    return names


class TestNameFormat:
    """Names must be spelled the way CPython spells them, since that is what the
    recorder wrote. A mismatch produces an empty intersection, which drops the
    step: drift fails in the dangerous direction."""

    def test_covers_every_shape_the_recordings_contain(self):
        assert names_of(SAMPLE, "sample.py") == {
            "<module>",
            "plain",
            "Holder",
            "Holder.method",
            "Holder.Nested",
            "Holder.Nested.deep",
            "outer",
            "outer.<locals>.closure",
            "<genexpr>",
            "<lambda>",
        }


class TestAttribution:
    def test_body_line_does_not_reach_module(self):
        # The precision property the whole filter rests on.
        assert names_for(SAMPLE, "inner = argument + 1") == {"plain"}

    def test_method_body_names_the_method_only(self):
        assert names_for(SAMPLE, "return self.field") == {"Holder.method"}

    def test_nested_class_method_keeps_the_full_chain(self):
        assert names_for(SAMPLE, "return 3") == {"Holder.Nested.deep"}

    def test_closure_body_uses_the_locals_form(self):
        assert names_for(SAMPLE, "return 4") == {"outer.<locals>.closure"}

    def test_module_level_statement_names_module(self):
        assert names_for(SAMPLE, "CONSTANT = 1") == {"<module>"}

    def test_def_header_reaches_both_scopes(self):
        # A signature edit changes what runs at import: the default is evaluated
        # there. Naming only the function would let an importing step be dropped.
        assert names_for(SAMPLE, "def plain(") == {"<module>", "plain"}

    def test_class_field_names_the_class_body(self):
        assert names_for(SAMPLE, "field: int = 2") == {"Holder"}

    def test_docstring_falls_back_to_the_innermost_scope(self):
        assert names_for(SAMPLE, '"""Docstring."""') == {"<module>"}

    def test_line_past_the_end_of_the_file_is_residue(self):
        names, residue = attribute(SAMPLE, "sample.py", {10_000})
        assert residue and not names


@pytest.fixture
def sample_repo(tmp_path: Path) -> Repo:
    root = tmp_path / "tmp_repo"
    root.mkdir()
    r = Repo(root)
    r.write("vllm/mod.py", SAMPLE)
    r.write("vllm/kernel.cu", "__global__ void k() {}\n")
    r.commit("base")
    return r


class TestBuild:
    def test_modified_body_yields_the_function_only(self, sample_repo: Repo):
        base = sample_repo.head()
        sample_repo.write(
            "vllm/mod.py",
            SAMPLE.replace("inner = argument + 1", "inner = argument + 2"),
        )
        head = sample_repo.commit("edit")

        (only,) = build(sample_repo.root, base, head).files
        assert only.path == "vllm/mod.py"
        assert only.status is Attribution.ATTRIBUTED
        assert only.names == {"plain"}
        assert not only.fail_open

    def test_deletion_keeps_the_base_side_names(self, sample_repo: Repo):
        base = sample_repo.head()
        (sample_repo.root / "vllm/mod.py").unlink()
        head = sample_repo.commit("delete")

        (only,) = build(sample_repo.root, base, head).files
        assert only.head_names == frozenset()
        assert "plain" in only.base_names and "<module>" in only.base_names

    def test_rename_reads_both_paths(self, sample_repo: Repo):
        base = sample_repo.head()
        sample_repo.git("mv", "vllm/mod.py", "vllm/moved.py")
        sample_repo.write(
            "vllm/moved.py", SAMPLE.replace("return inner", "return inner + 1")
        )
        head = sample_repo.commit("rename and edit")

        (only,) = build(sample_repo.root, base, head).files
        assert only.old_path == "vllm/mod.py" and only.path == "vllm/moved.py"
        assert only.base_names == {"plain"} and only.head_names == {"plain"}

    def test_non_python_is_nameless_not_failed(self, sample_repo: Repo):
        # The two empties must stay apart: this one is legitimate, a Python file
        # we could not read is not.
        base = sample_repo.head()
        sample_repo.write("vllm/kernel.cu", "__global__ void k() { int x = 1; }\n")
        head = sample_repo.commit("kernel")

        (only,) = build(sample_repo.root, base, head).files
        assert only.status is Attribution.NAMELESS
        assert only.names == frozenset()

    def test_unparsable_python_fails_open(self, sample_repo: Repo):
        base = sample_repo.head()
        sample_repo.write("vllm/mod.py", SAMPLE + "\ndef broken(:\n")
        head = sample_repo.commit("broken")

        (only,) = build(sample_repo.root, base, head).files
        assert only.status is Attribution.FAILED
        assert only.fail_open and only.note

    def test_file_outside_the_recorder_root_fails_open(self, sample_repo: Repo):
        # tests/ is real Python with real names, and no row can ever hold them.
        base = sample_repo.head()
        sample_repo.write("tests/test_thing.py", "def test_one():\n    assert True\n")
        head = sample_repo.commit("add test")

        (only,) = build(sample_repo.root, base, head).files
        assert only.status is Attribution.ATTRIBUTED
        assert only.names  # names exist
        assert not only.in_recorder_scope and only.fail_open

    def test_added_file_has_no_base_side(self, sample_repo: Repo):
        base = sample_repo.head()
        sample_repo.write("vllm/added.py", "def fresh():\n    return 1\n")
        head = sample_repo.commit("add")

        (only,) = build(sample_repo.root, base, head).files
        assert only.base_names == frozenset()
        assert only.head_names == {"<module>", "fresh"}


class TestLineEndings:
    """A carriage return at the end of a line changes nothing Python reads."""

    @staticmethod
    def _crlf(repo: Repo, path: str, text: str) -> None:
        (repo.root / path).write_bytes(text.replace("\n", "\r\n").encode())

    def test_a_crlf_rewrite_names_only_what_really_changed(self, sample_repo: Repo):
        """vllm#58669: every line rewritten to CRLF, one body really edited."""
        sample_repo.git("config", "core.autocrlf", "false")
        base = sample_repo.head()
        self._crlf(
            sample_repo,
            "vllm/mod.py",
            SAMPLE.replace("inner = argument + 1", "inner = argument + 2"),
        )
        head = sample_repo.commit("crlf and edit")

        (only,) = build(sample_repo.root, base, head).files
        assert only.status is Attribution.ATTRIBUTED
        assert only.names == {"plain"}, "not <module> and not every function"

    def test_a_line_ending_only_file_changes_no_function(self, sample_repo: Repo):
        sample_repo.git("config", "core.autocrlf", "false")
        base = sample_repo.head()
        self._crlf(sample_repo, "vllm/mod.py", SAMPLE)
        head = sample_repo.commit("crlf only")

        query = build(sample_repo.root, base, head)
        assert query.files == [], "no function changed, so no question to ask"
        assert query.eol_only == ["vllm/mod.py"]
        assert not query.fail_open

    def test_a_python_file_with_no_hunks_at_all_still_fails_open(
        self, sample_repo: Repo
    ):
        """A mode change has no hunks with or without line endings: still
        unanswerable, not mistaken for a line-ending-only change."""
        base = sample_repo.head()
        (sample_repo.root / "vllm/mod.py").chmod(0o755)
        head = sample_repo.commit("mode")

        (only,) = build(sample_repo.root, base, head).files
        assert only.status is Attribution.FAILED and only.fail_open


class TestBehaviourPreserving:
    """Changed lines whose code did not change name nothing, and a file whose
    change preserves behaviour leaves the query as inert."""

    BASE = (
        "import torch\n\n\n"
        "def fused(x, y):\n    return x + y\n\n\n"
        "def other(x):\n    return x * 2\n"
    )

    def _build(self, repo: Repo, text: str):
        base = repo.head()
        repo.write("vllm/mod.py", text)
        head = repo.commit("edit")
        return build(repo.root, base, head)

    @pytest.fixture
    def repo(self, tmp_path: Path) -> Repo:
        root = tmp_path / "r"
        root.mkdir()
        r = Repo(root)
        r.write("vllm/mod.py", self.BASE)
        r.commit("base")
        return r

    def test_a_return_annotation_alone_is_inert(self, repo: Repo):
        """vllm#58687: `) -> torch.Tensor:` on an undecorated function."""
        q = self._build(
            repo,
            self.BASE.replace("def fused(x, y):", "def fused(x, y) -> torch.Tensor:"),
        )
        assert q.files == [] and q.inert == ["vllm/mod.py"]

    def test_a_signature_split_over_lines_is_inert(self, repo: Repo):
        q = self._build(
            repo,
            self.BASE.replace("def fused(x, y):", "def fused(\n    x,\n    y,\n):"),
        )
        assert q.files == [] and q.inert == ["vllm/mod.py"]

    def test_a_real_body_change_keeps_only_that_function(self, repo: Repo):
        text = self.BASE.replace("def fused(x, y):", "def fused(x, y) -> torch.Tensor:")
        text = text.replace("return x * 2", "return x * 3")
        (only,) = self._build(repo, text).files
        assert only.function_names == {"other"}, "the annotated function did not change"

    def test_a_decorated_function_keeps_its_annotation_as_a_change(self, repo: Repo):
        """FastAPI, pydantic and friends read annotations at runtime."""
        base = self.BASE.replace("def fused", "@decorate\ndef fused")
        repo.write("vllm/mod.py", base)
        repo.commit("decorated")
        q = self._build(
            repo, base.replace("def fused(x, y):", "def fused(x, y) -> torch.Tensor:")
        )
        assert q.inert == [] and q.files and "<module>" in q.files[0].names

    def test_a_module_registering_a_custom_op_is_never_inert(self, repo: Repo):
        """Custom op registration infers the schema from annotations."""
        base = self.BASE + "\n\ndirect_register_custom_op('fused', fused)\n"
        repo.write("vllm/mod.py", base)
        repo.commit("op")
        q = self._build(
            repo, base.replace("def fused(x, y):", "def fused(x, y) -> torch.Tensor:")
        )
        assert q.inert == []

    def test_only_import_time_left_but_not_annotations_keeps_every_name(
        self, repo: Repo
    ):
        """All or nothing: dropping the unchanged function would leave a
        module-only change, which counts every importer as a use."""
        text = (
            self.BASE.replace("def fused(x, y):", "def fused(\n    x, y\n):")
            + "\nLIMIT = 3\n"
        )
        (only,) = self._build(repo, text).files
        assert "fused" in only.names and "<module>" in only.names
