"""A changed test helper routes by the names it changed, not the file.

vllm#56740 changed one method of RemoteVLLMServer in tests/utils.py. The
file-level closure crossed tests/conftest.py, which imports three unrelated
helpers from it, and selected nearly every test.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from ci_selector.codemap.state import DiffContext
from ci_selector.codemap.test_helpers import (
    affected_names,
    changed_names,
    is_helper,
    route,
)

HELPER = """\
import os


class Server:
    def start(self):
        return self._memory()

    def _memory(self):
        return 1


class OpenAIServer(Server):
    pass


def make_server():
    return OpenAIServer()


def wait_for_memory():
    return 0
"""

# The shape of tests/conftest.py: a function-local import of an unrelated
# helper, and every test beneath depends on it.
CONFTEST = """\
def pytest_configure():
    from tests.utils import wait_for_memory

    wait_for_memory()
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


class Graph:
    """The slice of the import graph `route` reads."""

    def __init__(self, imports: dict[str, set[str]]):
        self.reverse: dict[str, set[str]] = {}
        for src, dsts in imports.items():
            for dst in dsts:
                self.reverse.setdefault(dst, set()).add(src)

    def reverse_closure(self, files, include_boot=True):
        seen, stack = set(files), list(files)
        while stack:
            for src in self.reverse.get(stack.pop(), ()):
                if src not in seen:
                    seen.add(src)
                    stack.append(src)
        return seen


IMPORTS = {
    "tests/conftest.py": {"tests/utils.py"},
    "tests/test_server.py": {"tests/utils.py", "tests/conftest.py"},
    "tests/test_factory.py": {"tests/utils.py", "tests/conftest.py"},
    "tests/test_other.py": {"tests/conftest.py"},
    "tests/entrypoints/helpers.py": {"tests/utils.py"},
    "tests/entrypoints/test_via_helper.py": {
        "tests/entrypoints/helpers.py",
        "tests/conftest.py",
    },
}
FILES = {
    "tests/utils.py": HELPER,
    "tests/conftest.py": CONFTEST,
    "tests/test_server.py": "from tests.utils import OpenAIServer\n",
    "tests/test_factory.py": "from tests.utils import make_server\n",
    "tests/test_other.py": "def test_x():\n    pass\n",
    "tests/entrypoints/helpers.py": "from tests.utils import Server as S\n",
    "tests/entrypoints/test_via_helper.py": "from .helpers import S\n",
}


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    (tmp_path / "r").mkdir()
    r = Repo(tmp_path / "r")
    for path, text in FILES.items():
        r.write(path, text)
    r.commit()
    return r


def _route(repo: Repo, helper_text: str, imports=IMPORTS, extra=None):
    base = repo.git("rev-parse", "HEAD")
    repo.write("tests/utils.py", helper_text)
    for path, text in (extra or {}).items():
        repo.write(path, text)
    head = repo.commit()
    state = SimpleNamespace(
        repo=repo.root, full=SimpleNamespace(graph=Graph(imports)), auto_run_files=set()
    )
    ctx = DiffContext(base, head, {"tests/utils.py": "M"})
    return route(state, "tests/utils.py", ctx)


def test_a_method_edit_reaches_only_tests_naming_its_class_or_users(repo: Repo):
    """_memory changed: Server, its subclass, and the factory returning one
    are affected. conftest names none of them, so test_other is not reached."""
    routed = _route(repo, HELPER.replace("return 1", "return 2"))
    assert routed is not None
    tests, scripts, _ = routed
    assert tests == {
        "tests/test_server.py",
        "tests/test_factory.py",
        "tests/entrypoints/test_via_helper.py",
    }
    assert not scripts


def test_an_unrelated_function_edit_follows_conftest(repo: Repo):
    """wait_for_memory is what conftest imports, so every test runs it."""
    routed = _route(repo, HELPER.replace("return 0", "return 5"))
    tests = routed[0]
    assert "tests/test_other.py" in tests and "tests/test_server.py" in tests


def test_a_changed_import_counts_its_names(repo: Repo):
    """The import binds a name importers may take from the helper."""
    extra = {"tests/test_other.py": "from tests.utils import sys\n"}
    repo.write("tests/test_other.py", extra["tests/test_other.py"])
    repo.commit()
    imports = {**IMPORTS, "tests/test_other.py": {"tests/utils.py"}}
    edit = HELPER.replace("import os", "import os, sys")
    tests = _route(repo, edit, imports=imports)[0]
    assert tests == {"tests/test_other.py"}


def test_comments_only_route_nowhere(repo: Repo):
    edit = HELPER.replace("\n\nclass Server", "\n# note\n\nclass Server")
    tests, scripts, detail = _route(repo, edit)
    assert tests == set() and scripts == set()
    assert "only comments" in detail


def test_a_module_level_effect_keeps_the_file_level_claim(repo: Repo):
    """It runs on import and binds nothing, so every importer runs it."""
    assert _route(repo, HELPER + "\nos.environ['X'] = '1'\n") is None


def test_an_affected_name_used_on_import_keeps_the_file_level_claim(repo: Repo):
    repo.write("tests/utils.py", HELPER + "\nprint(Server)\n")
    repo.commit()
    edit = HELPER.replace("return 1", "return 2") + "\nprint(Server)\n"
    assert _route(repo, edit) is None


def test_a_star_importer_seeds(repo: Repo):
    """A star import need not spell where a name came from."""
    repo.write("tests/test_star.py", "from tests.utils import *\n")
    repo.commit()
    imports = {**IMPORTS, "tests/test_star.py": {"tests/utils.py"}}
    tests = _route(repo, HELPER.replace("return 1", "return 2"), imports=imports)[0]
    assert "tests/test_star.py" in tests


def test_nothing_naming_the_change_routes_nowhere(repo: Repo):
    """Strings are searched too, so a getattr would have named it."""
    imports = {"tests/conftest.py": {"tests/utils.py"}}
    tests, scripts, detail = _route(
        repo, HELPER.replace("return 1", "return 2"), imports=imports
    )
    assert tests == set() and scripts == set()
    assert "no importer names" in detail


def test_only_modified_helpers_route(repo: Repo):
    base = repo.git("rev-parse", "HEAD")
    state = SimpleNamespace(
        repo=repo.root, full=SimpleNamespace(graph=Graph(IMPORTS)), auto_run_files=set()
    )
    for status in ("A", "D", "R"):
        ctx = DiffContext(base, base, {"tests/utils.py": status})
        assert route(state, "tests/utils.py", ctx) is None
    assert route(state, "tests/utils.py", None) is None


@pytest.mark.parametrize(
    "path, expected",
    [
        ("tests/utils.py", True),
        ("tests/entrypoints/openai/utils.py", True),
        ("tests/conftest.py", False),
        ("tests/models/__init__.py", False),
        ("tests/test_utils.py", False),
        ("vllm/utils.py", False),
        ("tests/data/config.yaml", False),
    ],
)
def test_is_helper(path: str, expected: bool):
    assert is_helper(path) is expected


def test_changed_names_and_the_fixpoint():
    tree = ast.parse(HELPER)
    lines = {HELPER.splitlines().index("        return 1") + 1}
    assert changed_names(tree, lines) == {"Server"}
    assert affected_names([tree], {"Server"}) == {
        "Server",
        "OpenAIServer",
        "make_server",
    }


CONFTEST_BASE = """\
import pytest

from tests.utils import make_server


def _prompts():
    return ["a"]


@pytest.fixture
def prompts():
    return _prompts()


@pytest.fixture
def unused_prompts():
    return ["b"]


@pytest.fixture(autouse=True)
def reset():
    yield


def pytest_configure(config):
    pass
"""


def _route_conftest(repo: Repo, text: str):
    repo.write("tests/sub/conftest.py", CONFTEST_BASE)
    repo.write("tests/sub/test_a.py", "def test_a(prompts):\n    assert prompts\n")
    repo.write("tests/sub/test_b.py", "def test_b():\n    pass\n")
    base = repo.commit()
    repo.write("tests/sub/conftest.py", text)
    head = repo.commit()
    imports = {
        "tests/sub/test_a.py": {"tests/sub/conftest.py"},
        "tests/sub/test_b.py": {"tests/sub/conftest.py"},
    }
    state = SimpleNamespace(
        repo=repo.root, full=SimpleNamespace(graph=Graph(imports)), auto_run_files=set()
    )
    ctx = DiffContext(base, head, {"tests/sub/conftest.py": "M"})
    return route(state, "tests/sub/conftest.py", ctx)


def test_deleting_an_unused_fixture_routes_nowhere(repo: Repo):
    """vllm#58916: dead fixtures removed from tests/conftest.py."""
    text = CONFTEST_BASE.replace(
        '@pytest.fixture\ndef unused_prompts():\n    return ["b"]\n\n\n', ""
    )
    tests, _, _ = _route_conftest(repo, text)
    assert tests == set()


def test_a_helper_a_fixture_uses_reaches_the_tests_naming_the_fixture(repo: Repo):
    text = CONFTEST_BASE.replace('return ["a"]', 'return ["c"]')
    tests, _, _ = _route_conftest(repo, text)
    assert tests == {"tests/sub/test_a.py"}


@pytest.mark.parametrize(
    "old, new",
    [
        ("    yield\n", "    yield 1\n"),  # autouse fixture
        ("def pytest_configure(config):\n    pass", "def pytest_configure(config):\n    return"),
    ],
    ids=["autouse", "hook"],
)
def test_what_reaches_every_test_keeps_the_file_level_claim(repo: Repo, old, new):
    assert _route_conftest(repo, CONFTEST_BASE.replace(old, new)) is None
