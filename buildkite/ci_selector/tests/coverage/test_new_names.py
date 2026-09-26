"""New names reached only through changed code stop blocking narrowing."""

from __future__ import annotations

from pathlib import Path

import pytest
from ci_selector.coverage.changed_funcs import build
from ci_selector.coverage.new_names import resolve

from .helpers import Repo

BASE_MOD = """\
def plan(x):
    return x + 1


def untouched():
    return 0
"""


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    root = tmp_path / "r"
    root.mkdir()
    r = Repo(root)
    r.write("vllm/mod.py", BASE_MOD)
    r.commit("base")
    return r


def _resolve(repo: Repo, base: str, head: str, recorded: dict[str, set[str]]):
    """`recorded`: the names some row holds, per file. Everything else the
    diff names is unknown, as `unknown_names` would say."""
    query = build(repo.root, base, head)
    unresolved = {
        f.path: set(f.names) - recorded.get(f.path, set())
        for f in query.files
        if set(f.names) - recorded.get(f.path, set())
    }
    return resolve(repo.root, base, head, query, unresolved)


RECORDED = {"vllm/mod.py": {"<module>", "plan", "untouched"}}


def test_a_helper_called_only_from_a_changed_function_resolves(repo: Repo):
    """vllm#58684: new private helpers, all called from the changed plan()."""
    base = repo.head()
    repo.write(
        "vllm/mod.py",
        BASE_MOD.replace("return x + 1", "return _clamp(x) + 1")
        + "\n\ndef _clamp(x):\n    return max(x, 0)\n",
    )
    head = repo.commit("helper")
    assert _resolve(repo, base, head, RECORDED) == {"vllm/mod.py": {"_clamp"}}


def test_a_chain_of_new_helpers_resolves(repo: Repo):
    base = repo.head()
    repo.write(
        "vllm/mod.py",
        BASE_MOD.replace("return x + 1", "return _outer(x)")
        + "\n\ndef _outer(x):\n    return _inner(x) + 1\n"
        + "\n\ndef _inner(x):\n    return x\n",
    )
    head = repo.commit("chain")
    assert _resolve(repo, base, head, RECORDED) == {"vllm/mod.py": {"_outer", "_inner"}}


def test_a_function_only_a_test_calls_resolves(repo: Repo):
    """vllm#58664: a new wrapper only its new test calls."""
    base = repo.head()
    repo.write("vllm/mod.py", BASE_MOD + "\n\ndef fresh():\n    return 2\n")
    repo.write(
        "tests/test_fresh.py",
        "from vllm.mod import fresh\n\n\ndef test_fresh():\n    assert fresh() == 2\n",
    )
    head = repo.commit("fresh")
    assert _resolve(repo, base, head, RECORDED) == {"vllm/mod.py": {"fresh"}}


def test_a_module_level_reference_blocks(repo: Repo):
    """A registry entry runs on import, so every importer reaches it."""
    base = repo.head()
    repo.write(
        "vllm/mod.py",
        BASE_MOD + "\n\ndef fresh():\n    return 2\n\n\nREGISTRY = {'f': fresh}\n",
    )
    head = repo.commit("registry")
    assert _resolve(repo, base, head, RECORDED) == {}


def test_a_string_naming_it_blocks(repo: Repo):
    """How a dynamic lookup spells it."""
    base = repo.head()
    repo.write(
        "vllm/mod.py",
        BASE_MOD.replace("return x + 1", "return getattr(M, 'fresh')(x)")
        + "\n\ndef fresh(x):\n    return x\n",
    )
    head = repo.commit("getattr")
    assert _resolve(repo, base, head, RECORDED) == {}


def test_a_mention_in_a_changed_config_blocks_but_csrc_does_not(repo: Repo):
    base = repo.head()
    repo.write("vllm/mod.py", BASE_MOD + "\n\ndef fresh():\n    return 2\n")
    repo.write("csrc/fresh.cu", "void fresh() {}\n")
    head = repo.commit("csrc")
    assert _resolve(repo, base, head, RECORDED) == {"vllm/mod.py": {"fresh"}}, (
        "a C++ op shares its wrapper's name by design"
    )
    repo.write("vllm/config.yaml", "entry: fresh\n")
    head = repo.commit("yaml")
    assert _resolve(repo, base, head, RECORDED) == {}


def test_a_name_that_existed_at_base_is_never_resolved(repo: Repo):
    """Never recorded is missing data, not new code."""
    base = repo.head()
    repo.write("vllm/mod.py", BASE_MOD.replace("return 0", "return 1"))
    head = repo.commit("edit")
    recorded = {"vllm/mod.py": {"<module>", "plan"}}  # untouched never ran
    assert _resolve(repo, base, head, recorded) == {}


def test_a_new_file_reached_only_through_changed_code_resolves_whole(repo: Repo):
    """vllm#58686: a new module whose only importer changed in the same PR.
    Its module body resolves too, so the file stops being unseen."""
    base = repo.head()
    repo.write(
        "vllm/helpers.py",
        "def prepare(xs):\n    return list(x for x in xs)\n",
    )
    repo.write(
        "vllm/mod.py",
        "from vllm.helpers import prepare\n\n"
        + BASE_MOD.replace("return x + 1", "return prepare([x])"),
    )
    head = repo.commit("new file")
    got = _resolve(repo, base, head, RECORDED)
    assert got["vllm/helpers.py"] == {
        "<module>",
        "prepare",
        "prepare.<locals>.<genexpr>",
    }
