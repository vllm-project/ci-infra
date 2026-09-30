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

    def test_tests_files_are_in_scope_now_that_they_are_recorded(
        self, sample_repo: Repo
    ):
        """The recorder writes tests/ since build 91572; a row recorded before
        that holds no tests/ names, so they are unknown there and hold."""
        base = sample_repo.head()
        sample_repo.write("tests/test_thing.py", "def test_one():\n    assert True\n")
        head = sample_repo.commit("add test")

        (only,) = build(sample_repo.root, base, head).files
        assert only.status is Attribution.ATTRIBUTED
        assert only.names
        assert only.in_recorder_scope and not only.fail_open

    def test_file_outside_the_recorder_roots_fails_open(self, sample_repo: Repo):
        # benchmarks/ is real Python with real names, and no row holds them.
        base = sample_repo.head()
        sample_repo.write("benchmarks/bench.py", "def run():\n    return 1\n")
        head = sample_repo.commit("add bench")

        (only,) = build(sample_repo.root, base, head).files
        assert only.status is Attribution.ATTRIBUTED
        assert only.names
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

    def test_a_torch_compiled_model_is_never_inert(self, repo: Repo):
        """support_torch_compile infers dynamic dims from forward's
        annotations, and raises when it finds none."""
        base = self.BASE + (
            "\n\n@support_torch_compile\nclass Model:\n"
            "    def forward(self, x: torch.Tensor):\n        return x\n"
        )
        repo.write("vllm/mod.py", base)
        repo.commit("model")
        q = self._build(repo, base.replace("x: torch.Tensor", "x: list"))
        assert q.inert == [] and q.files and "Model.forward" in q.files[0].names

    @pytest.mark.parametrize(
        "nested, outer",
        [
            (
                (
                    "def outer(x):\n"
                    "    def inner(y: int) -> int:\n        return y\n"
                    "    return inner(x)\n"
                ),
                "outer",
            ),
            (
                (
                    "class C:\n    def run(self, x):\n"
                    "        def inner(y: int) -> int:\n            return y\n"
                    "        return inner(x)\n"
                ),
                "C.run",
            ),
        ],
        ids=["function", "method"],
    )
    def test_a_def_inside_a_function_keeps_its_annotation_as_a_change(
        self, repo: Repo, nested, outer
    ):
        """Its annotations run each time the enclosing function does: a name
        imported only under TYPE_CHECKING raises NameError there."""
        base = self.BASE + "\n\n" + nested
        repo.write("vllm/mod.py", base)
        repo.commit("nested")
        q = self._build(repo, base.replace("-> int:", "-> Tensor:"))
        (only,) = q.files
        assert q.inert == [] and only.function_names == {outer}

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


class TestDocstrings:
    """vllm#59008 reworded one function's docstring. That changes its code
    object, a constant of it, and nothing it runs."""

    BASE = (
        '"""Module."""\n\n\n'
        'def fused(x, y):\n    """Add them."""\n    return x + y\n\n\n'
        "def other(x):\n    return x * 2\n\n\n"
        'class Holder:\n    """Holds."""\n\n'
        '    def method(self):\n        """Three."""\n        return 3\n'
    )

    @pytest.fixture
    def repo(self, tmp_path: Path) -> Repo:
        root = tmp_path / "r"
        root.mkdir()
        r = Repo(root)
        r.write("vllm/mod.py", self.BASE)
        r.commit("base")
        return r

    def _build(self, repo: Repo, text: str, path: str = "vllm/mod.py"):
        base = repo.head()
        repo.write(path, text)
        head = repo.commit("edit")
        return build(repo.root, base, head)

    @pytest.mark.parametrize(
        "old, new",
        [
            ('"""Add them."""', '"""Add them.\n\n    Twice over.\n    """'),
            ('"""Three."""', '"""Always three."""'),
            ("def other(x):\n", 'def other(x):\n    """Double."""\n'),
        ],
        ids=["function", "method", "added"],
    )
    def test_a_function_docstring_alone_is_inert(self, repo: Repo, old, new):
        q = self._build(repo, self.BASE.replace(old, new))
        assert q.files == [] and q.inert == ["vllm/mod.py"]

    def test_a_docstring_and_an_annotation_together_are_inert(self, repo: Repo):
        text = self.BASE.replace('"""Add them."""', '"""Sum."""').replace(
            "def other(x):", "def other(x) -> int:"
        )
        q = self._build(repo, text)
        assert q.files == [] and q.inert == ["vllm/mod.py"]

    def test_a_docstring_beside_a_code_change_in_the_same_function(self, repo: Repo):
        text = self.BASE.replace('"""Add them."""', '"""Sum."""').replace(
            "return x + y", "return y + x"
        )
        (only,) = self._build(repo, text).files
        assert only.function_names == {"fused"}

    def test_a_code_change_elsewhere_keeps_the_docstring_edit(self, repo: Repo):
        """All or nothing: the file is judged whole, as for annotations."""
        text = self.BASE.replace('"""Add them."""', '"""Sum."""').replace(
            "return x * 2", "return x * 3"
        )
        (only,) = self._build(repo, text).files
        assert only.function_names == {"fused", "other"}

    def test_a_class_docstring_is_a_change(self, repo: Repo):
        """arg_utils.py turns config classes' docstrings into CLI help."""
        q = self._build(repo, self.BASE.replace('"""Holds."""', '"""Keeps."""'))
        assert q.inert == [] and q.files and "Holder" in q.files[0].names

    def test_a_module_docstring_is_a_change(self, repo: Repo):
        """Scripts pass `description=__doc__` to argparse."""
        q = self._build(repo, self.BASE.replace('"""Module."""', '"""Mod."""'))
        assert q.inert == [] and q.files and "<module>" in q.files[0].names

    def test_a_decorated_function_keeps_its_docstring_as_a_change(self, repo: Repo):
        """register_op in vllm/ir/op.py reads the docstring it is handed."""
        base = self.BASE.replace("def fused", "@register_op\ndef fused")
        repo.write("vllm/mod.py", base)
        repo.commit("decorated")
        q = self._build(repo, base.replace('"""Add them."""', '"""Sum."""'))
        assert q.inert == [] and q.files and "fused" in q.files[0].names

    @pytest.mark.parametrize(
        "reader, where",
        [
            ('HELP = f"{fused.__doc__}"\n', "vllm/cli.py"),
            ("HELP = inspect.getdoc(mod.fused)\n", "vllm/cli.py"),
            ("HELP = inspect.getdoc(\n    fused\n)\n", "vllm/cli.py"),
            ("p = ArgumentParser(description=fused.__doc__)\n", "tests/tool.py"),
        ],
        ids=["__doc__", "getdoc", "wrapped", "outside-vllm"],
    )
    def test_a_docstring_read_at_runtime_is_a_change(self, repo: Repo, reader, where):
        """arg_utils.py appends human_readable_int.__doc__ to CLI help. Ruff
        wraps a long call, putting the name on a line of its own."""
        repo.write(where, reader)
        repo.commit("reader")
        q = self._build(repo, self.BASE.replace('"""Add them."""', '"""Sum."""'))
        assert q.inert == [] and q.files and "fused" in q.files[0].names

    def test_a_reader_of_another_name_does_not_block(self, repo: Repo):
        repo.write("vllm/cli.py", 'HELP = f"{fused_other.__doc__}"\n\nfused(1, 2)\n')
        repo.commit("reader")
        q = self._build(repo, self.BASE.replace('"""Add them."""', '"""Sum."""'))
        assert q.files == [] and q.inert == ["vllm/mod.py"]

    def test_a_docstring_shared_with_the_body_is_not_skipped(self, repo: Repo):
        """CPython merges equal constants, so the docstring is also what this
        returns. Only the first constant differs, and it is a real change."""
        before = 'def tag():\n    """doc"""\n    return "doc"\n'
        after = before.replace("doc", "new")
        a, b = (compile(s, "m.py", "exec").co_consts[0] for s in (before, after))
        assert a.co_code == b.co_code and a.co_consts[1:] == b.co_consts[1:]
        assert a.co_consts[0] != b.co_consts[0]

        repo.write("vllm/tag.py", before)
        repo.commit("tag")
        q = self._build(repo, after, "vllm/tag.py")
        assert q.inert == [] and q.files and "tag" in q.files[0].names


def test_a_new_parameter_leaves_the_module_body_unchanged(tmp_path):
    """vllm#58828: a new `expert_map=None` parameter changes the module body's
    code object (the def builds a new function, with a new default), but
    import runs the same code. Only the function stays changed."""
    import subprocess

    from ci_selector.coverage.changed_funcs import build

    repo = tmp_path / "r"
    (repo / "vllm").mkdir(parents=True)

    def git(*a):
        return subprocess.run(
            ["git", "-C", str(repo), *a], capture_output=True, text=True, check=True
        ).stdout.strip()

    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    src = "import os\n\n\ndef op(x, is_gated: bool = True):\n    return f(x, is_gated)\n"
    (repo / "vllm/ops.py").write_text(src)
    git("add", "-A")
    git("commit", "-q", "-m", "base")
    base = git("rev-parse", "HEAD")
    (repo / "vllm/ops.py").write_text(
        src.replace("is_gated: bool = True)", "is_gated: bool = True, expert_map=None)")
        .replace("f(x, is_gated)", "f(x, is_gated, expert_map)")
    )
    git("commit", "-qam", "param")
    q = build(repo, base, git("rev-parse", "HEAD"))
    (f,) = q.files
    assert f.names == {"op"}, f.names

    # A default that is an expression runs at import: the body stays changed.
    (repo / "vllm/ops.py").write_text(src.replace("= True)", "= os.cpu_count())"))
    git("commit", "-qam", "expr default")
    head2 = git("rev-parse", "HEAD")
    q = build(repo, git("rev-parse", "HEAD~1"), head2)
    (f,) = q.files
    assert "<module>" in f.names


PLATFORM_SRC = """\
import vllm.envs as envs
from vllm.platforms import current_platform


def view(t):
    if envs.VLLM_PIN_VIEWS:
        t = t.pin_memory()
    if current_platform.is_xpu():
        if not t.is_pinned():
            t = t.contiguous()
        return xpu(t)
    elif current_platform.is_cuda_alike():
        return cuda(t)
    return t
"""


@pytest.mark.parametrize(
    "old, new, family",
    [
        ("t = t.contiguous()", "t = t.clone()", "xpu"),
        ("return cuda(t)", "return cuda(t, 1)", None),  # is_cuda_alike: two families
        ("    return t\n", "    return t + 0\n", None),  # outside every guard
        ("return xpu(t)", "from vllm import ops\n        return ops.xpu(t)", "xpu"),
        # `envs` turns local to view, so its first line raises UnboundLocalError.
        ("return xpu(t)", "import vllm.envs as envs\n        return xpu(t)", None),
        ("return xpu(t)", "yield\n        return xpu(t)", None),  # now a generator
    ],
    ids=[
        "xpu-branch",
        "cuda-alike",
        "unguarded",
        "binds-a-new-name",
        "rebinds-a-name-used-outside",
        "makes-a-generator",
    ],
)
def test_a_change_inside_one_platform_branch_is_tagged(tmp_path, old, new, family):
    """vllm#54874 changed only the XPU branch of a function 315 CUDA rows call.
    A line there that changes how the rest of the function resolves a name,
    or what kind of function it is, is not confined to it."""
    import subprocess

    from ci_selector.coverage.changed_funcs import build

    repo = tmp_path / "r"
    (repo / "vllm").mkdir(parents=True)

    def git(*a):
        return subprocess.run(
            ["git", "-C", str(repo), *a], capture_output=True, text=True, check=True
        ).stdout.strip()

    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (repo / "vllm/u.py").write_text(PLATFORM_SRC)
    git("add", "-A")
    git("commit", "-q", "-m", "base")
    base = git("rev-parse", "HEAD")
    (repo / "vllm/u.py").write_text(PLATFORM_SRC.replace(old, new))
    git("commit", "-qam", "edit")
    (f,) = build(repo, base, git("rev-parse", "HEAD")).files
    assert f.platform == family


SCOPES_SRC = """\
import vllm.envs as envs
from vllm.platforms import current_platform

k = 1

if current_platform.is_xpu():
    def step():
        yield 1


def step():
    if current_platform.is_xpu():
        step.calls = 0
    return 1


if current_platform.is_xpu():
    step.calls = 1


def pick(ts):
    if current_platform.is_xpu():
        ts = sorted(ts, key=lambda t: t.numel())
    size = lambda t: numel(t)
    if current_platform.is_xpu():
        ts = ts[:1]
    pinned = lambda: envs.VLLM_PIN_VIEWS
    return [size(t) for t in ts if pinned()]


class Views:
    if current_platform.is_xpu():
        pass
    k = 2
"""

MOVE_A_LAMBDA = [
    ("ts = sorted(ts, key=lambda t: t.numel())", "ts = list(ts)"),
    ("ts = ts[:1]", "ts = sorted(ts, key=lambda u: u.numel())"),
]


@pytest.mark.parametrize(
    "edits, family",
    [
        (MOVE_A_LAMBDA, "xpu"),
        # `numel` turns free in `size`, which raises NameError on CUDA.
        ([*MOVE_A_LAMBDA, ("ts = list(ts)", "from vllm.utils import numel")], None),
        ([("ts = ts[:1]", "import vllm.envs as envs")], None),  # read by `pinned`
        ([("pass", "global k")], None),  # `k = 2` now sets the module's `k`
        (
            [
                ("    def step():\n        yield 1", "    step = None"),
                ("step.calls = 0", "yield"),  # the `step` CUDA runs, a generator
                ("step.calls = 1", "def step():\n        return 1"),
            ],
            None,
        ),
    ],
    ids=[
        "moves-a-lambda",
        "moves-a-lambda-and-rebinds-for-another",
        "rebinds-for-a-closure",
        "globals-a-class-name",
        "moves-a-generator-and-makes-one",
    ],
)
def test_a_platform_branch_leaves_every_other_scope_as_it_was(tmp_path, edits, family):
    """A branch that rebinds a name a nested scope or a class body reads
    reaches past it too. Scopes of one name pair up in order, so a lambda
    moved from one XPU branch to another must not pair an unchanged lambda
    with the wrong one and hide a name it now reads free."""
    repo = Repo(tmp_path)
    repo.write("vllm/u.py", SCOPES_SRC)
    base = repo.commit("base")
    text = SCOPES_SRC
    for old, new in edits:
        assert text.count(old) == 1, old
        text = text.replace(old, new)
    repo.write("vllm/u.py", text)
    (f,) = build(repo.root, base, repo.commit("edit")).files
    assert f.platform == family
