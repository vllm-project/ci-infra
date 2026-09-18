# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""The unified parser-engine registry: each parser file is claimed and keyed by
its stem, so a change selects the steps exercising that parser rather than the
api_server hub's near-run-all.

The seal that makes it sound: claiming an engine only drops LAZY edges, so
every auto-run step whose test statically imports it survives. Coverage that
exists only at runtime is pinned by the mistral case below.
"""

from ci_selector.codemap.classify import colocation_routes, select
from helpers import drift_message


def _static_importer_tests(state, target):
    """Test files that transitively import `target` via module-level edges."""
    pr = state.full.plain_reverse
    seen, stack = {target}, [target]
    while stack:
        node = stack.pop()
        for src in pr.get(node, ()):
            if src not in seen:
                seen.add(src)
                stack.append(src)
    return {f for f in seen if f.startswith("tests/")}


def _floor_tests(state, target):
    """The tests a change to `target` may not stop selecting.

    Transitive static reach, except where colocation routes the file -- inside
    the import cycle that reach has collapsed (`harmony`'s 354 static importers
    against its siblings' ~23 is the collapse, not coverage it uniquely has),
    and past the hub gate the swap to test-routing is deliberate. There the
    floor is the tests importing the file DIRECTLY, which is the claim
    `colocated-tests` actually makes. `colocation_routes` is the rule's own
    trigger, so the floor choice here cannot drift from the rule.
    """
    if colocation_routes(state, target):
        return {
            f
            for f in state.full.plain_reverse.get(target, ())
            if f.startswith("tests/")
        }
    return _static_importer_tests(state, target)


def _steps_running(state, test_files):
    hit = set()
    for p in state.pipelines:
        for sid, st in p.targets.items():
            for t in st.targets:
                covered = (
                    t.path in test_files
                    if t.path.endswith(".py")
                    else any(f.startswith(t.path.rstrip("/") + "/") for f in test_files)
                )
                if covered:
                    hit.add(sid)
                    break
    return hit


def test_engine_set_derived_and_claimed(state):
    """The derivation finds the concrete parsers, not the base class or the
    package init; if the registry restructures, the specimens below move."""
    keyed = state.full.factories.parser_engine_entries
    assert len(keyed) >= 10, sorted(keyed)
    assert {"mistral", "qwen3", "harmony"} <= set(keyed)
    assert "__init__" not in keyed and "abstract_parser" not in keyed
    assert set(keyed.values()) <= state.full.factories.claims
    for stem, module in keyed.items():
        assert module == f"vllm/parser/{stem}.py"


def test_static_floor_no_under_selection(state):
    """The load-bearing seal: for every engine, every AUTO step running a test
    that statically imports it survives the tightened selection."""
    for module in sorted(state.full.factories.parser_engine_entries.values()):
        floor = _steps_running(state, _floor_tests(state, module)) & state.auto_step_ids
        sel = select(state, [module])
        assert not sel.run_all, module
        missing = floor - set(sel.selected)
        assert not missing, (module, sorted(missing))


def test_mistral_tightened_but_keeps_parser_coverage(state):
    """The other half of the check above: mistral coverage that no import edge
    holds up.

    mistral does have static importers, and those are already asserted by
    `test_static_floor_no_under_selection`. What is unguarded is the rest:
    tests that reach the parser only by naming it at runtime, so nothing in
    the graph keeps them alive when the engine claim drops its lazy edges.
    Derive them as the mistral-named tests the static floor does not already
    reach, and require every auto step running one to survive.
    """
    stem = "mistral"
    module = state.full.factories.parser_engine_entries[stem]
    # A directory named for the engine is where its suites live. A path
    # substring instead swept in model tests that never touch the parser.
    named = {f for f in state.catalog if stem in f.split("/")}
    runtime_only = named - _static_importer_tests(state, module)
    floor = _steps_running(state, runtime_only) & state.auto_step_ids
    assert len(floor) >= 3, drift_message(
        f"the {len(runtime_only)} runtime-only {stem} tests reach "
        f"{len(floor)} auto step(s)",
        "runtime-only coverage is the half a tightening can drop silently, "
        "since no import edge keeps it alive; the steps running it are what "
        "has to survive, and a count of FILES could not see them go -- parser "
        "tests growing module-level imports, or being deleted, left unrelated "
        "files holding the count up while nothing parser-shaped was pinned",
        "if the parser suites moved out of tests/parser/<stem>/ and "
        "tests/tool_use/<stem>/, point the name probe where they live now",
        "if they grew module-level imports of the parser the static floor now "
        "covers them, and the seal moved rather than broke",
    )
    sel = select(state, [module])
    assert not sel.run_all
    selected = set(sel.selected)
    assert len(selected) < 120, len(selected)  # was 252 (near-run-all)
    assert floor <= selected, sorted(floor - selected)
