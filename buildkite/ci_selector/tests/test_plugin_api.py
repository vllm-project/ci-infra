"""A diff that breaks vLLM's importable API selects the steps testing it from
outside.

The Ascend NPU job runs vllm-ascend's interface-compatibility test from its own
image, so no edge reaches it and it has no row. vllm#53558 removed
kv_cache_utils functions vllm-ascend calls and vllm#58997 removed
postprocess_mamba_all; the job failed on both, and nothing selected it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from ci_selector.codemap.claim import Claim
from ci_selector.codemap.classify import select
from ci_selector.codemap.plugin_api import breaks, diff_breaks, outside_steps
from ci_selector.codemap.state import DiffContext
from ci_selector.codemap.unions import _apply_plugin_api_union


BASE = """\
import os

LIMIT = 4


def check(config, groups, *, strict=False):
    pass


def spread(*args, **kwargs):
    pass


@dataclass
class Info:
    name: str
    size: int = 0


class Runner:
    mode = "eager"

    def _verify(self, logits, draft):
        pass

    @property
    def ready(self):
        return True

    @ready.setter
    def ready(self, value):
        pass


@triton.jit
def _kernel(x_ptr, n):
    pass
"""


def _edit(old: str, new: str) -> list[str]:
    assert old in BASE, old
    return breaks(BASE, BASE.replace(old, new))


@pytest.mark.parametrize(
    "old,new,why",
    [
        ("def check(", "def check_memory(", "check: removed"),
        ("LIMIT = 4\n", "", "LIMIT: removed"),
        ('    mode = "eager"\n', "", "Runner.mode: removed"),
        ("    size: int = 0\n", "", "Info.size: removed"),
        # vllm#52928 inserted a positional parameter ahead of existing ones.
        ("check(config, groups,", "check(config, layout, groups,", "check: positional"),
        # vllm#54442 added a required parameter to RejectionSampler._verify.
        (
            "_verify(self, logits, draft)",
            "_verify(self, logits, draft, sampled)",
            "requires sampled",
        ),
        ("strict=False", "strict", "requires strict"),
        (", *, strict=False", "", "dropped strict"),
        ("spread(*args, **kwargs)", "spread(**kwargs)", "lost *args"),
        ("spread(*args, **kwargs)", "spread(*args)", "lost **kwargs"),
        # vllm#58542 added a required field to BatchReqState.
        (
            "    size: int = 0\n",
            "    size: int = 0\n    seq: list\n",
            "new required field seq",
        ),
    ],
)
def test_a_break(old, new, why):
    found = _edit(old, new)
    assert any(why in b for b in found), found


@pytest.mark.parametrize(
    "old,new",
    [
        ("strict=False", "strict=False, verbose=False"),
        ("    size: int = 0\n", "    size: int = 0\n    seq: list = None\n"),
        (
            "def check(config, groups, *, strict=False):",
            "def check(config, groups, strict=False):",
        ),
        ("    pass\n", "    return 1\n"),
        # Only vLLM's own wrappers launch a kernel.
        ("def _kernel(x_ptr, n):", "def _kernel(n, x_ptr):"),
        ("@triton.jit\ndef _kernel(x_ptr, n):\n    pass\n", ""),
        # A moved name that its old home imports back is still importable.
        ("LIMIT = 4\n", "from vllm.limits import LIMIT\n"),
        (
            "@dataclass\nclass Info:\n    name: str\n    size: int = 0\n",
            "from vllm.info import Info\n",
        ),
        # A setter shares its getter's name, so only presence is checked.
        ("    @ready.setter\n    def ready(self, value):\n        pass\n", ""),
        ("import os\n", ""),
    ],
    ids=[
        "defaulted-param",
        "defaulted-field",
        "keyword-only-to-positional",
        "body",
        "kernel-reordered",
        "kernel-removed",
        "moved-name",
        "moved-class",
        "setter",
        "import",
    ],
)
def test_not_a_break(old, new):
    assert _edit(old, new) == []


def test_a_removed_class_is_reported_once():
    found = breaks(BASE, BASE.replace("class Runner:", "class Executor:"))
    assert found == ["Runner: removed"]


def test_a_deleted_module_removes_everything():
    found = breaks(BASE, None)
    assert {"check: removed", "Info: removed", "LIMIT: removed"} <= set(found)
    assert not any("_kernel" in b for b in found)


def test_a_name_bound_in_both_branches_is_still_there():
    before = (
        "try:\n    import x\n    HAS_X = True\nexcept ImportError:\n    HAS_X = False\n"
    )
    assert breaks(before, before.replace("    HAS_X = True\n", "")) == []
    assert breaks(before, "") == ["HAS_X: removed"]


class Repo:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True)
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
    r = Repo(tmp_path / "r")
    r.write("vllm/v1/utils.py", BASE)
    r.write("vllm/v1/other.py", "def other():\n    pass\n")
    r.write("tests/test_x.py", "def test_x():\n    pass\n")
    r.commit()
    return r


def _ctx(repo: Repo, base: str, head: str) -> DiffContext:
    status, renames = {}, {}
    for line in repo.git("diff", "--name-status", "-M", base, head).splitlines():
        parts = line.split("\t")
        if parts[0].startswith("R"):
            status[parts[2]] = "R"
            status[parts[1]] = "D"
            renames[parts[2]] = parts[1]
        else:
            status[parts[1]] = parts[0]
    return DiffContext(base, head, status, renames)


def test_a_moved_module_breaks_its_old_path_only(repo: Repo):
    """vllm#54025 moved flashinfer_utils.py under fused_moe/. Everything is
    still importable, from a path no plugin imports yet."""
    base = repo.git("rev-parse", "HEAD")
    repo.git("mv", "vllm/v1/utils.py", "vllm/v1/core_utils.py")
    ctx = _ctx(repo, base, repo.commit())
    assert ctx.status["vllm/v1/core_utils.py"] == "R"
    assert diff_breaks(repo.root, "vllm/v1/core_utils.py", ctx) == []
    assert "check: removed" in diff_breaks(repo.root, "vllm/v1/utils.py", ctx)


def test_only_a_vllm_module_can_break_the_api(repo: Repo):
    base = repo.git("rev-parse", "HEAD")
    repo.write("vllm/v1/new.py", "def new():\n    pass\n")
    repo.write("tests/test_x.py", "")
    ctx = _ctx(repo, base, repo.commit())
    assert diff_breaks(repo.root, "vllm/v1/new.py", ctx) == []
    assert diff_breaks(repo.root, "tests/test_x.py", ctx) == []
    assert diff_breaks(repo.root, "vllm/v1/utils.py", None) == []


def test_an_unreadable_side_counts_as_a_break(repo: Repo):
    """Selecting is the safe answer when the surface cannot be read."""
    base = repo.git("rev-parse", "HEAD")
    repo.write("vllm/v1/other.py", "def other(:\n")
    ctx = _ctx(repo, base, repo.commit())
    assert diff_breaks(repo.root, "vllm/v1/other.py", ctx)
    ghost = DiffContext(base, base, {"vllm/v1/ghost.py": "M"})
    assert diff_breaks(repo.root, "vllm/v1/ghost.py", ghost)


@pytest.fixture(scope="module")
def outside(state) -> set[str]:
    return {s for p in state.pipelines for s in outside_steps(p)}


@pytest.mark.drift
def test_the_outside_steps_are_the_ascend_jobs(outside):
    """Derived from the commands, not named: a step whose pytest target exists
    only inside its image. A new one is fine if it tests vLLM from outside;
    one that is a renamed in-tree test wants its command fixed instead."""
    assert outside == {"vllm_ci:ascend-npu-test", "vllm_rocm_ci:ascend-npu-test"}


def test_the_union_adds_the_outside_steps_unless_the_pipeline_runs_all(
    state, outside, repo
):
    base = repo.git("rev-parse", "HEAD")
    repo.write("vllm/v1/utils.py", BASE.replace("def check(", "def check_memory("))
    ctx = _ctx(repo, base, repo.commit())
    fake = SimpleNamespace(repo=repo.root, pipelines=state.pipelines)

    claim = _apply_plugin_api_union(
        fake, "vllm/v1/utils.py", Claim("graph", "d", step_ids={"x"}), ctx
    )
    assert outside and claim.step_ids == outside | {"x"}
    assert claim.droppable_step_ids == set(), "no row can speak for these"
    assert {claim.step_rule[s] for s in outside} == {"plugin-api"}
    assert all("check: removed" in claim.step_detail[s] for s in outside)
    assert claim.step_detail["x"] == "d", "the existing reason is pinned"

    run_all = Claim("fail-open", "d", run_all={"vllm_ci"})
    claim = _apply_plugin_api_union(fake, "vllm/v1/utils.py", run_all, ctx)
    assert claim.step_ids == {s for s in outside if not s.startswith("vllm_ci:")}

    body = _apply_plugin_api_union(fake, "vllm/v1/other.py", Claim("graph", "d"), ctx)
    assert body.step_ids == set()


def _commit_present(state, sha: str) -> bool:
    probe = subprocess.run(
        ["git", "-C", str(state.repo), "cat-file", "-e", f"{sha}^"],
        capture_output=True,
    )
    return probe.returncode == 0


def test_select_runs_the_outside_steps_for_a_removed_function(state, outside):
    """vllm#53941 deleted find_getitem_maybe from fx_utils.py."""
    sha = "6b15bea080ac64c9e123982b48cbbffe34957ec0"
    if not _commit_present(state, sha):
        pytest.skip("vllm#53941 not present locally (shallow clone)")
    path = "vllm/compilation/passes/fx_utils.py"
    sel = select(state, [path], base=f"{sha}^", head=sha)
    assert outside
    for sid in outside:
        assert "plugin-api" in sel.selected_rules.get(sid, []), sid
        assert sid in sel.selected_by_file[path]


def test_select_leaves_the_outside_steps_off_a_body_edit(state, outside):
    """vllm#56385 swapped H and W back in dummy_inputs.py: a body edit, no
    signature."""
    sha = "5ff50f3996d5b0f59dd48c73c1ec0f7d912b6466"
    if not _commit_present(state, sha):
        pytest.skip("specimen not present locally (shallow clone)")
    path = "vllm/multimodal/processing/dummy_inputs.py"
    sel = select(state, [path], base=f"{sha}^", head=sha)
    hit = {s for s in outside if "plugin-api" in sel.selected_rules.get(s, [])}
    assert not hit, {s: sel.selected[s] for s in hit}
