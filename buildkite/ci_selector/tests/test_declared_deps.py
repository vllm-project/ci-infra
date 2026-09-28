# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Routing without the declarations: the selector never reads the
hand-written source_file_dependencies lists. These check the derived legs still
reach what the lists name, which makes the lists an oracle here and never an
input."""

import ast
from pathlib import Path

import ci_selector
import pytest
from ci_selector.codemap import classify
from helpers import drift_message, steps_by_id

PKG = Path(ci_selector.__file__).parent


def test_declarations_are_read_only_to_parse_and_compare():
    """The steps' declared lists are parsed, and the replica of today's rules
    and the crosscheck compare against them. A read anywhere else is the
    selector using them as an input."""
    attr_reads: dict[str, int] = {}
    for py in sorted(PKG.rglob("*.py")):
        rel = py.relative_to(PKG).as_posix()
        tree = ast.parse(py.read_text())
        a = sum(
            1
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and node.attr == "source_file_dependencies"
        )
        if a:
            attr_reads[rel] = a
    assert set(attr_reads) == {
        "codemap/pipeline/buildkite.py",
        "validate/crosscheck.py",
        "validate/generator_replica.py",
    }, f"Step.source_file_dependencies read outside parsing: {attr_reads}"


def test_requirements_route_by_what_the_steps_run(state):
    """A requirements file routes by what the steps run: the nightly-torch
    check installs its file from a script."""
    claim = classify._classify_requirements(state, "requirements/lint.txt")
    assert claim is not None and claim.rule == "requirements" and not claim.run_all
    claim = classify._classify_requirements(
        state, "requirements/test/nightly-torch.txt"
    )
    assert claim is not None and claim.step_ids & state.auto_step_ids


# --- lane 1: derived reference legs -----------------------------------------


def _cargo_suite(state) -> set[str]:
    return {
        sid
        for p in state.pipelines
        for sid, st in p.targets.items()
        if any(s.endswith("run-rust-frontend-cargo-ci.sh") for s in st.scripts_seen)
    } & state.auto_step_ids


def test_rust_files_keep_the_cargo_suite_without_declarations(state):
    cargo = _cargo_suite(state)
    assert cargo, "no step invokes run-rust-frontend-cargo-ci.sh; probe died"
    claim = classify._classify(state, "rust/Cargo.toml", None)
    assert cargo <= claim.step_ids


def _docker_build_text(state) -> str:
    """Everything the image builds actually run, concatenated."""
    return "\n".join(
        f.read_text(errors="ignore")
        for f in sorted((state.repo / "docker").rglob("*"))
        if f.is_file()
    )


def _steps_matched_by_target_literals(
    state, keys: set[str], build_text: str
) -> dict[str, set[tuple[str, str]]]:
    """step_id -> the (target file, string literal) pairs naming one of `keys`.

    A step's own pytest targets feed `searchable`, so a test asserting on a
    command string reaches the raw-key leg the way a command does. That makes
    this a fourth way to reach a step, beside cargo, the gate env vars and the
    always-run floor, rather than an exemption from them.

    The literal has to be one the image builds really run. Matching the key
    anywhere would excuse any step whose target merely names a toolchain file
    in a docstring or a skip reason.
    """
    literals = state.full.graph.string_literals
    evidence: dict[str, set[tuple[str, str]]] = {}
    for p in state.pipelines:
        for sid, st in p.targets.items():
            for t in st.targets:
                if not t.path.endswith(".py"):
                    continue
                for lit in literals.get(t.path, ()):
                    if lit not in build_text:
                        continue
                    for key in keys:
                        if key in lit:
                            evidence.setdefault(sid, set()).add((t.path, lit))
    return evidence


def test_rust_reference_leg_over_match_floor(state):
    """The leg reaches the cargo suite plus steps that run anyway; anything
    else is an over-match to inspect.

    A total partition, so every step in the leg is either explained by a named
    mechanism or reported. The explanations print their evidence: an id list
    said which steps were excused without saying why, and went stale the moment
    vLLM re-keyed them.
    """
    from ci_selector.handwritten import RUST_GATE_ENV_VARS, RUST_TOOLCHAIN_FILES

    leg = (
        state.keys.steps_naming_raw({"rust/", *RUST_TOOLCHAIN_FILES})
        & state.auto_step_ids
    )
    cargo = _cargo_suite(state)
    assert cargo <= leg
    gate = state.keys.steps_naming_raw(set(RUST_GATE_ENV_VARS))
    # The docker build-metadata steps land here because their own pytest target
    # asserts on `bash build_rust.sh`, which the Dockerfile really runs, so a
    # rust change really does reach them.
    by_test = _steps_matched_by_target_literals(
        state, set(RUST_TOOLCHAIN_FILES), _docker_build_text(state)
    )
    assert len(by_test) <= 4, drift_message(
        f"{len(by_test)} steps are excused by a target literal: {sorted(by_test)}",
        "every excused step is one this partition stops inspecting; the "
        "exemption is meant for the docker build-metadata pair, and growing "
        "unwatched it absorbs the very over-matches the case exists to report",
        "if another build-metadata job is real, raise the bound and name it",
        "if ordinary suites started matching through their targets, the "
        "raw-key leg widened -- fix that rather than the bound",
    )
    by_id = steps_by_id(state)
    unexplained = {
        s for s in leg - cargo - gate - set(by_test) if not by_id[s].always_runs
    }
    explained = "; ".join(f"{s}: {sorted(ev)}" for s, ev in sorted(by_test.items()))
    assert not unexplained, (
        f"rust reference leg over-matches: {sorted(unexplained)}\n"
        f"explained by a target literal: {explained}"
    )


def test_untargeted_example_asset_routes_by_workdir_affinity(state):
    from ci_selector.codemap.step_refs import _direct_step_refs

    probe = None
    for p in sorted(state.repo.glob("examples/**/*.jinja")):
        rel = p.relative_to(state.repo).as_posix()
        if not _direct_step_refs(state, rel):
            probe = rel
            break
    if probe is None:
        pytest.skip("every examples jinja is directly referenced")
    claim = classify._classify(state, probe, None)
    assert not claim.run_all, f"{probe} escalated to run-all"
    affinity = classify._workdir_affinity_steps(state, probe)
    assert affinity & state.auto_step_ids
    assert affinity <= claim.step_ids


def test_graph_known_untargeted_example_keeps_its_tree_step(state):
    from ci_selector.codemap.state import _graph_known
    from ci_selector.codemap.step_refs import _direct_step_refs

    probe = None
    for p in sorted(state.repo.glob("examples/**/*.py")):
        rel = p.relative_to(state.repo).as_posix()
        if _graph_known(state, rel) and not _direct_step_refs(state, rel):
            probe = rel
            break
    if probe is None:
        pytest.skip("every graph-known example is directly referenced")
    claim = classify._classify(state, probe, None)
    affinity = classify._workdir_affinity_steps(state, probe)
    assert affinity & state.auto_step_ids
    assert affinity <= claim.step_ids
    assert not (affinity & claim.droppable_step_ids), (
        "affinity steps must not be droppable"
    )


# --- lane 2: the native-tests rule -----------------------------------------


def test_native_tests_routes_a_joined_tu_by_its_op_joints(state):
    fired = 0
    for p in sorted(state.native_ops.file_ops):
        if not state.native_ops.owns(p):
            continue
        claim = classify._classify_native_tests(state, p)
        if claim is None:
            continue
        fired += 1
        assert claim.rule == "native-tests"
        assert claim.step_ids & state.auto_step_ids
        assert not claim.droppable_step_ids and not claim.evidence_paths
    assert fired, "no owned csrc file fires native-tests; rule is dead"


def test_native_tests_stands_down_with_the_op_switch_off(state, monkeypatch):
    """Off means the op parse is not trusted, so this rule declines too
    rather than using evidence the switch just turned off."""
    monkeypatch.setenv("CI_SELECTOR_CSRC_OPS", "off")
    for p in sorted(state.native_ops.file_ops):
        if state.native_ops.owns(p):
            assert classify._classify_native_tests(state, p) is None
            return
    raise AssertionError("no owned csrc file to probe")


def test_native_tests_declines_outside_its_scope(state):
    assert classify._classify_native_tests(state, "csrc/cpu/cpu_attn.cpp") is None
    assert classify._classify_native_tests(state, "cmake/hipify.py") is None
    assert classify._classify_native_tests(state, "CMakeLists.txt") is None


def test_native_tests_declines_without_derived_evidence(state):
    """A csrc file with no ops and no references falls through, rather than
    firing on its family alone."""
    tracked = {p for p in state.native_ops.file_ops if state.native_ops.owns(p)}
    for p in sorted(state.repo.glob("csrc/**/*")):
        rel = p.relative_to(state.repo).as_posix()
        if not state.native_ops.owns(rel) or rel in tracked or not p.is_file():
            continue
        if state.native_ops.test_files_for(rel):
            continue
        from ci_selector.codemap.step_refs import _direct_step_refs

        if _direct_step_refs(state, rel):
            continue
        assert classify._classify_native_tests(state, rel) is None
        return
    pytest.skip("every owned csrc file currently derives evidence")


def test_cuda_only_kernel_drops_its_declared_intel_steps(state):
    """A cuda-only kernel does not select an xpu step that declares it: the
    build map proves the file never compiles into that step."""
    if state.preflight.unmapped_devices:
        pytest.skip("the build map stood down, so every family is kept")
    # Test steps only: the xpu image builder carries no device name, so it
    # rides the remainder under cuda scoping.
    intel = {
        s
        for s in state.auto_step_ids
        if s.startswith("vllm_intel_ci:") and "image-build" not in s
    }
    from helpers import declaring_steps

    probe = None
    for p in sorted(state.native_ops.file_ops):
        if not state.native_ops.owns(p):
            continue
        if state.build_map.families.get(p) != frozenset({"cuda"}):
            continue
        if not (declaring_steps(state, p) & intel):
            continue
        claim = classify._classify_native_tests(state, p)
        # Only promised where the intel steps ride the declaration alone. A
        # file whose op tests intel also runs keeps them, which is not a miss.
        if claim is None or claim.step_ids & intel:
            continue
        probe = p
        break
    assert probe, "no cuda-only file whose intel steps are declaration-only"
    claim = classify._classify(state, probe, None)
    assert not (claim.step_ids & intel), "the intel step should have gone"
    assert claim.step_ids & state.auto_step_ids, "nothing is selected"


def test_release_file_with_live_declarers_never_selects_nothing(state):
    """A release-referenced file with live declarers must not select nothing."""
    from helpers import declaring_steps

    live = [
        p
        for p in sorted(state.release_refs)
        if declaring_steps(state, p, auto_only=True)
    ]
    assert live, "no release-referenced file with live declarers; probe died"
    for p in live[:2]:
        claim = classify._classify(state, p, None)
        assert claim.run_all or claim.step_ids & state.auto_step_ids, p
