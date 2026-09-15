# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Dynamic-site classification and ambiguity reporting."""

import pytest
from ci_selector.validate.dynamic_sites import classify_dynamic_sites
from helpers import HW, drift_message


@pytest.mark.drift
def test_no_unclassified_dynamic_sites_at_head(vllm_repo, full):
    """Every non-literal dynamic import at HEAD either leaves the repo or has a
    parser reading the table behind it."""
    fg = full
    classified = classify_dynamic_sites(fg.graph.dynamic_sites, fg.graph.table_files)
    assert classified.unclassified == [], drift_message(
        "These do a dynamic import the import graph cannot follow:\n"
        + "\n".join(
            f"    {s.file}:{s.lineno}  ({s.func})" for s in classified.unclassified
        ),
        "Any test reachable only through that import will not be selected, so a "
        "change to what it loads can ship without its tests running.",
        "it loads something outside the repo (a plugin, an optional package, a "
        "name from user config): add the file to DYNAMIC_IMPORT_FILES in " + HW,
        "it reads a table that lives in the repo: teach a parser in "
        "ci_selector/codemap/graph/factories.py to read that table, which links "
        "the edge properly instead of giving up on it",
    )


def test_bare_import_ambiguities_reported_not_silent(full):
    fg = full
    # tests/v1/determinism `from utils import`: sibling wins, and the clash
    # with the indexed top-level tests/utils.py is surfaced.
    clashes = {(file, name) for file, name, _sib, _other in fg.graph.ambiguities}
    assert any(
        file.startswith("tests/v1/determinism/") and name == "utils"
        for file, name in clashes
    ), clashes


def test_new_site_under_former_blanket_is_unclassified(vllm_repo):
    """A NEW dynamic-import site anywhere (even under a formerly-blanketed prefix
    like tests/models/) must land UNCLASSIFIED, not silently blessed."""
    from ci_selector.codemap.graph.imports import DynamicSite

    site = DynamicSite(
        "tests/models/language/generation/dispatch.py", 1, "import_module"
    )
    classified = classify_dynamic_sites([site], set())
    assert classified.unclassified == [site]


def test_reverse_gate_flags_entries_with_no_live_import(vllm_repo):
    """Not vacuous: with no live sites every hand-listed entry is flagged. An
    entry that outlives its import pre-approves the next one to land there,
    which checking only for unclassified sites never catches."""
    from ci_selector.handwritten import DYNAMIC_IMPORT_FILES
    from ci_selector.validate.dynamic_sites import unused_external_entries

    assert set(unused_external_entries([])) == set(DYNAMIC_IMPORT_FILES)


@pytest.mark.drift
def test_no_unused_hand_list_entries_at_head(vllm_repo, full):
    from ci_selector.validate.dynamic_sites import unused_external_entries

    dead = unused_external_entries(full.graph.dynamic_sites)
    assert dead == [], drift_message(
        "These are listed in DYNAMIC_IMPORT_FILES but hold no dynamic import "
        f"any more, or no longer exist: {dead}",
        "A listed file is exempt from the unclassified check. One that outlived "
        "its import silently exempts the next import added to it.",
        f"the file moved: update the path in DYNAMIC_IMPORT_FILES in {HW}",
        f"the import is gone for good: delete the entry from {HW}",
    )


@pytest.mark.drift
def test_every_recording_parser_contributes_a_table_file(full):
    """The derived half's detection floor. table_files is what now vouches for
    a dynamic import, so a parser that quietly stopped recording would bless
    nothing and read exactly like a clean run."""
    files = full.graph.table_files
    for owner, marker in (
        ("register calls", "vllm/distributed/kv_transfer/kv_connector/factory.py"),
        ("lazy parser tables", "vllm/tool_parsers/__init__.py"),
        ("qualname enums", "vllm/v1/attention/backends/registry.py"),
        ("vllm/__init__ MODULE_ATTRS", "vllm/__init__.py"),
        ("lazy export tables", "vllm/transformers_utils/configs/__init__.py"),
        ("pkgutil enumerators", "vllm/kernels/helion/ops/__init__.py"),
        ("model registry", "vllm/model_executor/models/registry.py"),
        ("quant methods", "vllm/model_executor/layers/quantization/__init__.py"),
        ("platform methods", "vllm/platforms/interface.py"),
    ):
        assert marker in files, drift_message(
            f"The {owner} parser recorded no table this run "
            f"(expected it to read {marker}).",
            "table_files is what now vouches for a dynamic import. A parser that "
            "stopped matching blesses nothing and reads exactly like a clean run, "
            "so its file's import goes unclassified with no explanation.",
            "the table moved or was renamed in vLLM: update the parser in "
            "ci_selector/codemap/graph/ to find it again",
            "the anchor path or table name changed: update it in " + HW,
        )


def test_module_level_constant_reads_like_a_literal(full):
    """`import_module(SOME_CONST)` where SOME_CONST is a module-level string is
    exactly as knowable as the literal form, so it is not a hole. Specimen:
    the modelexpress loader, which used to need a hand-list entry."""
    loader = "vllm/model_executor/model_loader/modelexpress_loader.py"
    assert not [s for s in full.graph.dynamic_sites if s.file == loader]


def test_function_local_constant_reads_like_a_literal(full):
    """Scope does not change how readable a name is: one bound in a function
    and imported a line later is the module-level case."""
    site = "tests/kernels/test_mhc_tilelang_jit.py"
    assert not [s for s in full.graph.dynamic_sites if s.file == site]
    edges = full.graph.imports.get(site, set())
    assert "vllm/model_executor/kernels/mhc/tilelang_kernels.py" in edges


def test_a_name_meaning_two_things_is_not_folded():
    """The floor under the widening. A name two scopes bind differently is
    dropped rather than guessed, or we invent an edge."""
    import ast

    from ci_selector.codemap.graph.imports import _module_string_consts

    consts = _module_string_consts(
        ast.parse(
            "SAME = 'vllm.a'\n"
            "def one():\n"
            "    SAME = 'vllm.a'\n"
            "    LOCAL = 'vllm.b'\n"
            "def two():\n"
            "    SPLIT = 'vllm.c'\n"
            "def three():\n"
            "    SPLIT = 'vllm.d'\n"
            "def four(SHADOWED):\n"
            "    pass\n"
            "SHADOWED = 'vllm.e'\n"
        )
    )
    assert consts["LOCAL"] == "vllm.b"
    assert consts["SAME"] == "vllm.a"
    assert "SPLIT" not in consts
    assert "SHADOWED" not in consts


def test_unresolvable_path_inside_our_own_packages_is_still_a_site(vllm_repo, tmp_path):
    """A constant naming something outside vllm/tests/benchmarks proves the
    import leaves the repo. One naming a path INSIDE them that resolves to
    nothing is a broken or built-elsewhere target, and used to vanish here."""
    import ast

    from ci_selector.codemap.graph.imports import ImportGraph, _resolve_call
    from ci_selector.codemap.repo import build_module_index

    index = build_module_index(vllm_repo)
    for source, expect_site in (
        ('importlib.import_module("scipy.linalg")', False),
        ('importlib.import_module("vllm.no.such.module")', True),
    ):
        graph = ImportGraph()
        call = ast.parse(source).body[0].value
        _resolve_call(call, index, graph, "vllm/x.py", {})
        assert bool(graph.dynamic_sites) is expect_site, source


def test_lazy_loader_targets_get_an_edge(full):
    """LazyLoader is a fourth way to import dynamically, and its module is a
    plain literal in the third argument. It went unread until 2026-08-19, so
    these three edges did not exist and the targets lost an importer."""
    for src, dst in (
        ("vllm/utils/mistral.py", "vllm/tokenizers/mistral.py"),
        (
            "vllm/config/speculative.py",
            "vllm/model_executor/layers/quantization/__init__.py",
        ),
        ("vllm/config/model.py", "vllm/model_executor/layers/quantization/__init__.py"),
    ):
        assert dst in full.graph.imports.get(src, set()), (src, dst)


def test_lazy_loader_external_targets_stay_silent(full):
    """Most LazyLoader targets are third-party packages, which resolve to
    nothing and must not become unclassified sites."""
    sites = [s for s in full.graph.dynamic_sites if s.func == "LazyLoader"]
    assert sites == [], [(s.file, s.lineno) for s in sites]


TEST_FILE = "tests/test_dispatch.py"

_DISPATCH = (
    "import pytest\n"
    "from importlib import import_module\n"
    "\n"
    '@pytest.mark.parametrize("backend", ["amd", "nvidia", "xpu"])\n'
    "def test_mtp(backend):\n"
    '    import_module(f"vllm.models.{backend}.mtp")\n'
)
_BACKEND_MODULES = (
    "vllm.models.amd.mtp",
    "vllm.models.nvidia.mtp",
    "vllm.models.xpu.mtp",
)


def _mini_dispatch_repo(root, source, *, modules=(), setup_py=None):
    """One test file doing a dynamic import, beside the vllm modules it can
    name. Omitting `setup_py` leaves no setup.py at all."""
    from ci_selector.codemap.graph.imports import build_graph
    from ci_selector.codemap.repo import build_module_index

    (root / "vllm").mkdir(parents=True)
    (root / "vllm/__init__.py").write_text("")
    for module in modules:
        path = root / (module.replace(".", "/") + ".py")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
    (root / "tests").mkdir()
    (root / TEST_FILE).write_text(source)
    if setup_py is not None:
        (root / "setup.py").write_text(setup_py)
    return build_graph(root, build_module_index(root))


def _line_of(source: str, needle: str) -> int:
    return next(i for i, line in enumerate(source.splitlines(), 1) if needle in line)


def _call_line(source: str) -> int:
    return _line_of(source, "import_module(")


def test_fstring_over_parametrize_links_every_option(tmp_path):
    """The decorator names every backend the import runs under, so each is a
    real edge."""
    graph = _mini_dispatch_repo(tmp_path, _DISPATCH, modules=_BACKEND_MODULES)
    assert graph.imports[TEST_FILE] == {
        "vllm/models/amd/mtp.py",
        "vllm/models/nvidia/mtp.py",
        "vllm/models/xpu/mtp.py",
    }
    assert graph.dynamic_sites == []


def test_partial_resolve_keeps_the_edge_and_reports_once(tmp_path):
    """Every candidate really runs, so the ones naming nothing still count as
    missed even though another resolved. Reported once per call site."""
    graph = _mini_dispatch_repo(tmp_path, _DISPATCH, modules=_BACKEND_MODULES[:1])
    assert graph.imports[TEST_FILE] == {"vllm/models/amd/mtp.py"}
    assert [(s.file, s.lineno, s.func) for s in graph.dynamic_sites] == [
        (TEST_FILE, _call_line(_DISPATCH), "import_module")
    ]


def test_product_over_the_cap_falls_back_to_a_site(tmp_path):
    """A dispatch too wide to list gives up loudly, where truncating it would
    drop real edges in silence."""
    from ci_selector.codemap.graph.imports import MAX_MODULE_CANDIDATES

    side = int(MAX_MODULE_CANDIDATES**0.5) + 1
    values = [f"v{i}" for i in range(side)]
    source = (
        "import pytest\n"
        "from importlib import import_module\n"
        "\n"
        f'@pytest.mark.parametrize("major", {values!r})\n'
        f'@pytest.mark.parametrize("minor", {values!r})\n'
        "def test_wide(major, minor):\n"
        '    import_module(f"vllm.models.{major}.{minor}")\n'
    )
    # Every target exists, so an uncapped expansion would link them all.
    graph = _mini_dispatch_repo(
        tmp_path,
        source,
        modules=[f"vllm.models.{a}.{b}" for a in values for b in values],
    )
    assert graph.imports.get(TEST_FILE, set()) == set()
    assert len(graph.dynamic_sites) == 1


def test_bare_name_over_the_cap_falls_back_to_a_site(tmp_path):
    """The cap belongs to the call site, not to f-strings, so a bare name has
    to give up exactly where an f-string does."""
    from ci_selector.codemap.graph.imports import MAX_MODULE_CANDIDATES

    modules = [f"vllm.models.m{i}" for i in range(MAX_MODULE_CANDIDATES + 1)]
    source = (
        "import pytest\n"
        "from importlib import import_module\n"
        "\n"
        f'@pytest.mark.parametrize("mod", {modules!r})\n'
        "def test_wide(mod):\n"
        "    import_module(mod)\n"
    )
    graph = _mini_dispatch_repo(tmp_path, source, modules=modules)
    assert graph.imports.get(TEST_FILE, set()) == set()
    assert len(graph.dynamic_sites) == 1


def test_fstring_leaving_the_repo_stays_silent(tmp_path):
    """A package outside the repo has no edge to miss, however many
    candidates it expands to."""
    source = (
        "import pytest\n"
        "from importlib import import_module\n"
        "\n"
        '@pytest.mark.parametrize("sub", ["nn", "fx"])\n'
        "def test_torch(sub):\n"
        '    import_module(f"torch.{sub}")\n'
    )
    graph = _mini_dispatch_repo(tmp_path, source)
    assert graph.imports.get(TEST_FILE, set()) == set()
    assert graph.dynamic_sites == []


def test_bare_parametrized_name_still_expands(tmp_path):
    """A bare name is the base case of the f-string path, and links as it
    always did."""
    options = list(_BACKEND_MODULES[:2])
    source = (
        "import pytest\n"
        "from importlib import import_module\n"
        "\n"
        f'@pytest.mark.parametrize("mod", {options!r})\n'
        "def test_mtp(mod):\n"
        "    import_module(mod)\n"
    )
    graph = _mini_dispatch_repo(tmp_path, source, modules=_BACKEND_MODULES[:2])
    assert graph.imports[TEST_FILE] == {
        "vllm/models/amd/mtp.py",
        "vllm/models/nvidia/mtp.py",
    }
    assert graph.dynamic_sites == []


def test_a_name_in_both_tables_yields_both():
    """Either table alone can miss a target the other one names, so both are
    read."""
    import ast

    from ci_selector.codemap.graph.imports import _module_strings

    arg = ast.parse("name").body[0].value
    assert _module_strings(arg, {"name": "vllm.a"}, {"name": {"vllm.b"}}) == {
        "vllm.a",
        "vllm.b",
    }
    assert _module_strings(arg, {}, {}) is None


def test_compiled_extension_resolves_only_while_setup_py_builds_it(tmp_path):
    """`vllm._moe_C_stable_libtorch` is a CMake target; no .py can ever back
    it, so it is resolved rather than a hole. Two neighbours in the same file
    keep the two legs from being satisfied by a blanket "bless anything under
    vllm that will not resolve": a name setup.py never declares still reports,
    and an ordinary module still gets its edge. That edge is the only one
    assertable here, since a compiled target has no file to point at."""
    source = (
        "import importlib\n"
        "\n"
        "def _available():\n"
        '    importlib.import_module("vllm._moe_C_stable_libtorch")\n'
        '    importlib.import_module("vllm._never_built_C")\n'
        '    importlib.import_module("vllm.models.amd.mtp")\n'
    )
    declared = (
        'ext_modules.append(CMakeExtension(name="vllm._moe_C_stable_libtorch"))\n'
    )
    undeclared = _line_of(source, "vllm._never_built_C")
    compiled = _line_of(source, "vllm._moe_C_stable_libtorch")
    for case, (setup_py, expect_sites) in enumerate(
        ((declared, [undeclared]), ("ext_modules = []\n", [compiled, undeclared]))
    ):
        graph = _mini_dispatch_repo(
            tmp_path / str(case),
            source,
            modules=["vllm.models.amd.mtp"],
            setup_py=setup_py,
        )
        assert graph.imports.get(TEST_FILE, set()) == {"vllm/models/amd/mtp.py"}
        assert sorted(s.lineno for s in graph.dynamic_sites) == expect_sites, setup_py
