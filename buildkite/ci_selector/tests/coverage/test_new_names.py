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


def test_a_lambda_inside_a_lambda_follows_its_nested_parent(repo: Repo):
    """The inner lambda's parent is the outer lambda, itself nested. Resolving
    once in dict order left the inner one unknown whenever it came first."""
    base = repo.head()
    repo.write(
        "vllm/mod.py",
        BASE_MOD.replace("return x + 1", "return (lambda y: (lambda z: z)(y))(x) + 1"),
    )
    head = repo.commit("nested lambdas")
    query = build(repo.root, base, head)
    names = {n for f in query.files for n in f.names}
    inner = "plan.<locals>.<lambda>.<locals>.<lambda>"
    assert inner in names, names
    unresolved = {"vllm/mod.py": {"plan.<locals>.<lambda>", inner}}
    for order in (list(unresolved["vllm/mod.py"]), sorted(unresolved["vllm/mod.py"])):
        got = resolve(repo.root, base, head, query, {"vllm/mod.py": set(order)})
        assert got == {"vllm/mod.py": {"plan.<locals>.<lambda>", inner}}


ERRORS = """\
class GenerationError(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.status = 500
"""
RETRYABLE = """

class Retryable(GenerationError):
    def __init__(self, message="retry"):
        super().__init__(message)
        self.status = 503
"""
RAISES = "from vllm.errors import Retryable\n\n" + BASE_MOD.replace(
    "return x + 1", "if x < 0:\n        raise Retryable()\n    return x + 1"
)
ERRORS_RECORDED = {
    **RECORDED,
    "vllm/errors.py": {"<module>", "GenerationError", "GenerationError.__init__"},
}


def _add_errors(repo: Repo, base_files: dict[str, str], head_files: dict[str, str]):
    for path, text in base_files.items():
        repo.write(path, text)
    base = repo.commit("base errors")
    for path, text in head_files.items():
        repo.write(path, text)
    return _resolve(repo, base, repo.commit("head errors"), ERRORS_RECORDED)


def test_a_new_class_dunder_follows_its_class(repo: Repo):
    """vllm#58975: a new exception with an __init__, raised only from the
    changed plan(). By leaf, the unchanged GenerationError.__init__'s
    super().__init__ kept it unknown while the class resolved."""
    got = _add_errors(
        repo,
        {"vllm/errors.py": ERRORS},
        {"vllm/errors.py": ERRORS + RETRYABLE, "vllm/mod.py": RAISES},
    )
    assert got == {"vllm/errors.py": {"Retryable", "Retryable.__init__"}}


def test_a_dataclass_dunder_follows_its_class(repo: Repo):
    """dataclass hands the class back and keeps nothing, so __post_init__
    still runs only where the class is named."""
    head = "from dataclasses import dataclass\n\n\n" + ERRORS
    budget = "\n\n@dataclass\nclass Budget:\n    limit: int\n\n"
    budget += "    def __post_init__(self):\n        self.limit = max(self.limit, 0)\n"
    mod = "from vllm.errors import Budget\n\n" + BASE_MOD.replace(
        "return x + 1", "return Budget(x).limit + 1"
    )
    got = _add_errors(
        repo,
        {"vllm/errors.py": head},
        {"vllm/errors.py": head + budget, "vllm/mod.py": mod},
    )
    assert got == {"vllm/errors.py": {"Budget", "Budget.__post_init__"}}


REGISTRY = """\
KINDS = []


def register(cls):
    KINDS.append(cls)
    return cls


def build_all():
    return [kind("x") for kind in KINDS]


"""
REGISTERING = ERRORS.replace(
    "class GenerationError(Exception):\n",
    "class GenerationError(Exception):\n"
    "    def __init_subclass__(cls):\n        KINDS.append(cls)\n\n",
)
SCAN = """\
from vllm.errors import GenerationError


def every():
    return [kind("x") for kind in GenerationError.__subclasses__()]
"""


@pytest.mark.parametrize(
    "base_files, head_files",
    [
        pytest.param(
            {"vllm/errors.py": REGISTRY + ERRORS},
            {
                "vllm/errors.py": REGISTRY
                + ERRORS
                + RETRYABLE.replace("class", "@register\nclass")
            },
            id="decorator",
        ),
        pytest.param(
            {"vllm/errors.py": REGISTRY + REGISTERING},
            {"vllm/errors.py": REGISTRY + REGISTERING + RETRYABLE},
            id="base-init-subclass",
        ),
        pytest.param(
            {"vllm/errors.py": ERRORS + "\n\nclass Meta(type):\n    pass\n"},
            {
                "vllm/errors.py": ERRORS
                + "\n\nclass Meta(type):\n    pass\n"
                + RETRYABLE.replace(
                    "(GenerationError)", "(GenerationError, metaclass=Meta)"
                )
            },
            id="metaclass",
        ),
        pytest.param(
            {
                "vllm/base.py": ERRORS,
                "vllm/errors.py": "from vllm.base import GenerationError\n",
            },
            {
                "vllm/errors.py": "from vllm.base import GenerationError\n" + RETRYABLE,
            },
            id="base-from-another-file",
        ),
        pytest.param(
            {"vllm/errors.py": ERRORS, "vllm/scan.py": SCAN},
            {"vllm/errors.py": ERRORS + RETRYABLE},
            id="base-subclasses-scan",
        ),
    ],
)
def test_a_class_handed_over_unnamed_keeps_its_dunders_unknown(
    repo: Repo, base_files, head_files
):
    """A decorator, a base's __init_subclass__ or metaclass, or a scan of a
    base's __subclasses__() gets the class without naming it, and unchanged
    code can build it from there (vllm/v1/metrics/perf.py registers every
    ComponentMetrics subclass). The class still resolves; its __init__ must
    not follow it."""
    got = _add_errors(repo, base_files, {**head_files, "vllm/mod.py": RAISES})
    assert got == {"vllm/errors.py": {"Retryable"}}


def test_an_enum_keeps_its_dunders_unknown(repo: Repo):
    """An Enum builds its members as the class is made, so every importer
    runs its __init__."""
    head = "import enum\n\n\n" + ERRORS
    mode = "\n\nclass Mode(enum.Enum):\n    A = 1\n\n"
    mode += "    def __init__(self, value):\n        self.code = value * 10\n"
    mod = "from vllm.errors import Mode\n\n" + BASE_MOD.replace(
        "return x + 1", "return Mode(x).code"
    )
    got = _add_errors(
        repo,
        {"vllm/errors.py": head},
        {"vllm/errors.py": head + mode, "vllm/mod.py": mod},
    )
    assert got == {"vllm/errors.py": {"Mode"}}


def test_a_dunder_of_an_unresolved_class_stays_unknown(repo: Repo):
    """Following the class cuts both ways: a __call__ nothing spells, and a
    helper only it calls, resolved on their own before, although a registry
    naming the class at import reaches them."""
    call = RETRYABLE + "\n    def __call__(self):\n        return _code()\n"
    call += "\n\ndef _code():\n    return 503\n\n\nKINDS = [Retryable]\n"
    got = _add_errors(
        repo,
        {"vllm/errors.py": ERRORS},
        {"vllm/errors.py": ERRORS + call, "vllm/mod.py": RAISES},
    )
    assert got == {}


def test_a_new_dunder_of_an_existing_class_stays_unknown(repo: Repo):
    """Instances of a class that existed at base can be anywhere, and `==`
    spells no __eq__, so a new one never resolves."""
    out = "\n\nclass Out:\n    def __init__(self, v):\n        self.v = v\n"
    eq = "\n    def __eq__(self, other):\n        return self.v == other.v\n"
    got = _add_errors(
        repo,
        {"vllm/errors.py": ERRORS + out},
        {"vllm/errors.py": ERRORS + out + eq},
    )
    assert got == {}


def test_a_helper_only_a_new_dunder_calls_resolves(repo: Repo):
    """The dunder resolves in the same fixpoint as everything else, so what
    only it calls can borrow it."""
    got = _add_errors(
        repo,
        {"vllm/errors.py": ERRORS},
        {
            "vllm/errors.py": ERRORS
            + RETRYABLE.replace("self.status = 503", "self.status = _code()")
            + "\n\ndef _code():\n    return 503\n",
            "vllm/mod.py": RAISES,
        },
    )
    assert got == {"vllm/errors.py": {"Retryable", "Retryable.__init__", "_code"}}


def test_a_class_body_vouches_for_nothing(repo: Repo):
    """A class body runs on import, so what it calls every importer runs:
    here Retryable.__init__, though Holder itself resolves."""
    holder = "\n\nclass Holder:\n    default = Retryable()\n"
    mod = "from vllm.errors import Holder\n\n" + BASE_MOD.replace(
        "return x + 1", "if x < 0:\n        raise Holder.default\n    return x + 1"
    )
    got = _add_errors(
        repo,
        {"vllm/errors.py": ERRORS},
        {"vllm/errors.py": ERRORS + RETRYABLE + holder, "vllm/mod.py": mod},
    )
    assert got == {"vllm/errors.py": {"Holder"}}


def test_a_new_files_module_body_vouches_for_nothing(repo: Repo):
    """The same for a new file: its body resolves, being reached only by
    import, but an instance built there is built by every importer."""
    base = repo.head()
    repo.write(
        "vllm/plugin.py",
        "class Plugin:\n    def __init__(self):\n        self.ready = True\n"
        "\n\nPLUGIN = Plugin()\n",
    )
    repo.write(
        "vllm/mod.py",
        "from vllm.plugin import PLUGIN\n\n"
        + BASE_MOD.replace("return x + 1", "return x + PLUGIN.ready"),
    )
    head = repo.commit("plugin")
    assert _resolve(repo, base, head, RECORDED) == {"vllm/plugin.py": {"<module>"}}


def test_a_decorator_or_default_runs_where_the_def_does(repo: Repo):
    """Both share the def's first line with the function, but run on import
    here, not when plan() is called."""
    base = repo.head()
    tracer = "class Tracer:\n    def __init__(self):\n        self.calls = 0\n"
    tracer += "\n    def __call__(self, fn):\n        return fn\n\n\n"
    repo.write("vllm/mod.py", tracer + "@Tracer()\n" + BASE_MOD)
    head = repo.commit("decorator")
    assert _resolve(repo, base, head, RECORDED) == {}
    repo.write(
        "vllm/mod.py",
        "def _default():\n    return 1\n\n\n"
        + BASE_MOD.replace("def plan(x):", "def plan(x, k=_default()):"),
    )
    head = repo.commit("default")
    assert _resolve(repo, base, head, RECORDED) == {}


# What runs a def without a caller in sight: a decorator handed the function,
# the interpreter through a dunder, or a lookup by name on an instance. None
# of those is a reference, so "no reference outside known code" says nothing.

VALIDATORS = """\
def model_validator(mode):
    def wrap(fn):
        return fn

    return wrap
"""
REQUEST = """\
from vllm.fw import model_validator


class Request:
    @model_validator(mode="before")
    @classmethod
    def check_prompt(cls, data):
        return data
"""
REQUEST_RECORDED = {"vllm/proto.py": {"<module>", "Request", "Request.check_prompt"}}
HOOKS = """\
HOOKS = []


def hook(fn):
    HOOKS.append(fn)
    return fn


def fire():
    return [h() for h in HOOKS]
"""


def test_a_validator_nothing_calls_stays_unknown(repo: Repo):
    """Pydantic runs a @model_validator on every construction of its class,
    and nothing in the diff names it."""
    repo.write("vllm/fw.py", VALIDATORS)
    repo.write("vllm/proto.py", REQUEST)
    base = repo.commit("request")
    new = '\n    @model_validator(mode="before")\n    @classmethod\n'
    new += "    def check_messages(cls, data):\n"
    new += '        if not data.get("messages"):\n            raise ValueError\n'
    new += "        return data\n"
    repo.write("vllm/proto.py", REQUEST + new)
    head = repo.commit("validator")
    assert _resolve(repo, base, head, REQUEST_RECORDED) == {}


def test_a_registered_function_nothing_names_stays_unknown(repo: Repo):
    """An existing registry decorator keeps the function,
    and the unchanged fire() calls it on every use."""
    repo.write("vllm/hooks.py", HOOKS)
    base = repo.commit("hooks")
    repo.write("vllm/hooks.py", HOOKS + "\n\n@hook\ndef on_start():\n    return 1\n")
    head = repo.commit("on_start")
    recorded = {
        "vllm/hooks.py": {"<module>", "hook", "fire", "fire.<locals>.<listcomp>"}
    }
    assert _resolve(repo, base, head, recorded) == {}


def test_a_rebound_builtin_name_is_not_transparent(repo: Repo):
    """A file that binds `staticmethod` itself can register through it."""
    hooks = HOOKS.replace("def hook(fn):", "def staticmethod(fn):")
    repo.write("vllm/hooks.py", hooks)
    base = repo.commit("hooks")
    repo.write(
        "vllm/hooks.py", hooks + "\n\n@staticmethod\ndef on_start():\n    return 1\n"
    )
    head = repo.commit("on_start")
    recorded = {
        "vllm/hooks.py": {
            "<module>",
            "staticmethod",
            "fire",
            "fire.<locals>.<listcomp>",
        }
    }
    assert _resolve(repo, base, head, recorded) == {}


def test_a_module_dunder_stays_unknown(repo: Repo):
    """PEP 562: the interpreter calls a module's __getattr__ on any missing
    attribute, from any importer."""
    base = repo.head()
    repo.write(
        "vllm/mod.py",
        BASE_MOD + "\n\ndef __getattr__(name):\n    raise AttributeError(name)\n",
    )
    head = repo.commit("module getattr")
    assert _resolve(repo, base, head, RECORDED) == {}


def test_a_hook_a_library_base_calls_by_a_name_no_file_spells_stays_unknown(
    repo: Repo,
):
    """ast.NodeVisitor.visit builds "visit_" + the node's type name and calls
    it on every walk. No file in the repo has to spell visit_Assign for the
    new method to run, so a fresh name proves nothing."""
    walker = "import ast\n\n\nclass Walker(ast.NodeVisitor):\n"
    walker += "    def visit_Call(self, node):\n        self.generic_visit(node)\n"
    repo.write("vllm/walker.py", walker)
    base = repo.commit("walker")
    repo.write(
        "vllm/walker.py",
        walker + "\n    def visit_Assign(self, node):\n        raise RuntimeError\n",
    )
    head = repo.commit("visit_Assign")
    recorded = {"vllm/walker.py": {"<module>", "Walker", "Walker.visit_Call"}}
    assert _resolve(repo, base, head, recorded) == {}


def test_a_builtin_decorator_hides_no_caller(repo: Repo):
    """staticmethod hands the function back to the class, so on a new sealed
    class references still find every caller."""
    base = repo.head()
    repo.write(
        "vllm/mod.py",
        BASE_MOD.replace("return x + 1", "return Job.flag() + x")
        + "\n\nclass Job:\n    @staticmethod\n    def flag():\n        return 1\n",
    )
    head = repo.commit("staticmethod")
    got = _resolve(repo, base, head, RECORDED)
    assert {"Job", "Job.flag"} <= got.get("vllm/mod.py", set()), got


def test_a_method_of_a_new_sealed_class_follows_the_class(repo: Repo):
    """Its instances exist only where the class is named, so an unrelated
    `.run` in an unchanged function of a changed file does not hold it back."""
    other = "def go(task):\n    return task.run()\n\n\ndef count():\n    return 0\n"
    repo.write("vllm/other.py", other)
    base = repo.commit("other")
    repo.write(
        "vllm/mod.py",
        BASE_MOD.replace("return x + 1", "return Job().run() + x")
        + "\n\nclass Job:\n    def run(self):\n        return 1\n",
    )
    repo.write("vllm/other.py", other.replace("return 0", "return 1"))
    head = repo.commit("job")
    recorded = {**RECORDED, "vllm/other.py": {"<module>", "go", "count"}}
    got = _resolve(repo, base, head, recorded)
    assert {"Job", "Job.run"} <= got.get("vllm/mod.py", set()), got


def test_a_decorated_inner_function_follows_its_parent(repo: Repo):
    """Whatever the decorator does with it happens when plan() runs, so the
    steps that ran plan() cover it."""
    base = repo.head()
    repo.write(
        "vllm/mod.py",
        "import functools\n\n\n"
        + BASE_MOD.replace(
            "    return x + 1",
            "    @functools.cache\n    def inner():\n        return x\n\n"
            "    return inner() + 1",
        ),
    )
    head = repo.commit("inner")
    assert _resolve(repo, base, head, RECORDED) == {
        "vllm/mod.py": {"plan.<locals>.inner"}
    }


def test_a_function_moved_into_a_module_that_re_exported_it_stays_unknown(
    repo: Repo,
):
    """runner.py imported `step_all` from mod.py at base, and the unchanged
    child.py imports it from runner.py. Moving the body into runner.py adds
    no code object anyone had, yet the name was there all along."""
    repo.write("vllm/mod.py", BASE_MOD + "\n\ndef step_all():\n    return 1\n")
    repo.write("vllm/runner.py", "from vllm.mod import step_all\n")
    repo.write(
        "vllm/child.py",
        "from vllm.runner import step_all\n\n\ndef go():\n    return step_all()\n",
    )
    base = repo.commit("re-export")
    repo.write("vllm/mod.py", BASE_MOD)
    repo.write("vllm/runner.py", "def step_all():\n    return 1\n")
    head = repo.commit("move")
    recorded = {**RECORDED, "vllm/runner.py": {"<module>"}}
    assert _resolve(repo, base, head, recorded) == {}


def test_a_dunder_of_a_class_inside_a_function_follows_the_function(repo: Repo):
    """The class exists only once plan() runs, so whatever runs its
    __init__ ran plan() first."""
    base = repo.head()
    repo.write(
        "vllm/mod.py",
        BASE_MOD.replace(
            "    return x + 1",
            "    class Box:\n        def __init__(self, v):\n            self.v = v\n\n"
            "    return Box(x).v + 1",
        ),
    )
    head = repo.commit("local class")
    got = _resolve(repo, base, head, RECORDED)
    assert "plan.<locals>.Box.__init__" in got.get("vllm/mod.py", set()), got
