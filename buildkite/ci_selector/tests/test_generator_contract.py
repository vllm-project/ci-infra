# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""What we still have to check about ci-infra's generator, now that we import it.

Most of section 4 of `handwritten.py` used to be a hand copy of the generator,
kept honest by downloading that repo and diffing against it. The generator is
now a sibling package here and is imported, so those copies are gone and the
checks that watched them went with them. Nothing replaces them: a value taken
from the generator cannot disagree with the generator.

What is left is the part importing does not solve.

  literals   the generator writes some facts as bare literals inside a function
             body, with no name to import. We read those out of its live source
             with `inspect.getsource`.
  schema     two of its shapes are untyped or deliberately loose, so its own
             call sites are the whole schema. We check we model all of them.
  behaviour  three places where our code is not a copy and cannot become one,
             because it answers a different question or takes our own types.
             Those we run against the real function on the same inputs.

Behaviour rather than text is what keeps this self-clearing: our replica is
written in our own names and types, so comparing source could only say
"something moved". Running both and comparing outputs answers the real
question, so upstream moving goes red and fixing our code goes green.
"""

import itertools

import pytest
from generator_source import (
    attr_assignment,
    literals_in,
    method_arg,
    mirror_override_keys,
)
from helpers import drift_message

import amd as amd_mod
import buildkite_step
import pipeline_generator as generator_mod
import step as step_mod
from ci_selector import handwritten

HW = "ci_selector/handwritten.py"

COST = (
    "These are the values the generator does not expose as a name, so they are "
    "the only part of section 4 that can still go stale. They sit on the main "
    "selection path: getting one wrong means modelling a generator that does "
    "not exist, which shows up as jobs we fail to select."
)

PATHS = (
    "",
    "/",
    "a",
    "a/",
    "a/b",
    "a/b.py",
    "ab",
    "a//b",
    "docs/x.md",
    "x.md",
    "mkdocs.yaml",
    "docs",
)

DEP_SETS = (
    None,
    [],
    ["a"],
    ["a", "b"],
    ["!a"],
    ["a", "!a/b"],
    ["vllm"],
    ["vllm", "!vllm/x.py"],
    ["/"],
    [""],
)


def ours(const):
    """Our constant as a sorted list of strings, so a lone string and a
    one-element tuple compare equal to each other."""
    value = getattr(handwritten, const)
    return [value] if isinstance(value, str) else sorted(value)


# ------------------------------------------------------------------ literals


@pytest.mark.drift
def test_the_image_build_prefix_matches_the_generators_own_test():
    """Upstream grants always-run by key prefix, written inline as a
    `startswith`. Read the literal out of that call rather than trusting the
    string to appear somewhere in the package."""
    try:
        upstream = method_arg(buildkite_step._step_should_run, "startswith")
    except LookupError as exc:
        pytest.fail(
            drift_message(
                f"Cannot find the always-run prefix test in ci-infra: {exc}",
                COST,
                "upstream restructured _step_should_run: re-read it and update "
                "the query in tests/generator_source.py",
            )
        )
    assert ours("IMAGE_BUILD_KEY_PREFIX") == [upstream], drift_message(
        f"IMAGE_BUILD_KEY_PREFIX is {ours('IMAGE_BUILD_KEY_PREFIX')}, but "
        f"ci-infra grants always-run on {upstream!r}.",
        "These steps build the images every other step waits on. Getting the "
        "prefix wrong either runs them never or runs them always.",
        f"update IMAGE_BUILD_KEY_PREFIX in {HW} to {upstream!r}",
    )


@pytest.mark.drift
def test_the_default_working_dir_matches_the_generators_own():
    """Upstream sets it inline while reading a job dir, so there is no
    constant to compare; take the assignment."""
    try:
        upstream = attr_assignment(step_mod.read_steps_from_job_dir, "working_dir")
    except LookupError as exc:
        pytest.fail(
            drift_message(
                f"Cannot find the working-dir default in ci-infra: {exc}",
                COST,
                "upstream restructured read_steps_from_job_dir: re-read it and "
                "update the query in tests/generator_source.py",
            )
        )
    assert ours("DEFAULT_WORKING_DIR") == [upstream], drift_message(
        f"DEFAULT_WORKING_DIR is {ours('DEFAULT_WORKING_DIR')}, but ci-infra "
        f"defaults steps to {upstream!r}.",
        "Step working dirs are absolute paths under it, mapped back to "
        "repo-relative. A wrong root sends every relative test target astray.",
        f"update DEFAULT_WORKING_DIR in {HW} to {upstream!r}",
    )


@pytest.mark.drift
def test_the_container_workspace_still_matches_the_amd_pods():
    """We keep our own literal because the generator has no generic constant
    for it, only `AMD_NATIVE_WORKSPACE`, which means the AMD pod's volume mount
    and is free to move on its own. They are the same string today; pin that so
    the day they part ways is a red test rather than a silent divergence."""
    assert handwritten.CONTAINER_WORKSPACE == amd_mod.AMD_NATIVE_WORKSPACE, (
        drift_message(
            f"CONTAINER_WORKSPACE is {handwritten.CONTAINER_WORKSPACE!r} but "
            f"amd.AMD_NATIVE_WORKSPACE is {amd_mod.AMD_NATIVE_WORKSPACE!r}.",
            "Absolute step working_dirs are mapped back to repo-relative "
            "against this root. A wrong one sends every relative target astray.",
            f"check which of the two moved, then update CONTAINER_WORKSPACE in {HW}",
            "if they have genuinely diverged in meaning, delete this test and "
            "say so where the constant is defined",
        )
    )


@pytest.mark.drift
def test_the_docs_only_rule_uses_exactly_our_three_values():
    """All three of ours are the string literals of one upstream function, so
    the whole rule can be compared at once rather than value by value."""
    upstream = literals_in(generator_mod.is_docs_only_change)
    mine = set(
        ours("DOCS_ONLY_PREFIXES")
        + ours("DOCS_ONLY_SUFFIXES")
        + ours("DOCS_ONLY_EXACT")
    )
    assert mine == upstream, drift_message(
        f"The docs-only rule diverged. We use {sorted(mine)}; ci-infra's "
        f"is_docs_only_change uses {sorted(upstream)}.",
        "A whole-diff docs-only answer emits nothing at all, so this predicate "
        "is the one place we can take CI to zero steps. Reading it wrong in "
        "either direction is the most expensive mistake in the tool.",
        f"update DOCS_ONLY_PREFIXES, _SUFFIXES or _EXACT in {HW}",
    )


# -------------------------------------------------------------------- schema


@pytest.mark.drift
def test_every_mirror_key_the_generator_reads_is_modelled():
    """Upstream types a mirror override as `Dict[str, Any]`, so its `amd[...]`
    reads are the only schema there is. A key we do not model is force-selected
    on every step that carries it."""
    keys = mirror_override_keys(
        buildkite_step.convert_group_step_to_buildkite_step,
        buildkite_step._get_amd_mirror_effective_step,
    )
    # Guard the guard: an upstream rename would empty the scan, and an empty
    # set is a subset of anything.
    assert len(keys) >= 15, drift_message(
        f"Only {len(keys)} mirror override keys found, so the check below is "
        "comparing against almost nothing.",
        "An extraction that stopped working reads exactly like a generator "
        "that stopped adding fields.",
        "the mirror dict was renamed or the reads moved: check MIRROR_VAR and "
        "mirror_override_keys() in tests/generator_source.py",
    )
    assert not keys - handwritten.MIRROR_OVERRIDABLE, drift_message(
        "ci-infra reads mirror override keys we do not model: "
        f"{sorted(keys - handwritten.MIRROR_OVERRIDABLE)}.",
        "Preflight force-selects every step carrying an unmodelled key, and a "
        "forced step is not droppable, so this is a cost no coverage evidence "
        "can lift.",
        f"add them to MIRROR_OVERRIDABLE in {HW}",
        "if the key changes what the step runs: also teach _expand_mirror in "
        "ci_selector/codemap/pipeline/buildkite.py to read it",
    )


@pytest.mark.drift
def test_every_step_field_the_generator_declares_is_modelled():
    """The top-level half of the mirror check above. This model really is
    typed, so its declared fields are the schema outright.

    Deliberately not derived: `KNOWN_STEP_FIELDS` stays hand-written so that a
    field the generator newly declares fails here instead of becoming known to
    us the moment upstream adds it.
    """
    fields = set(step_mod.Step.model_fields)
    assert not fields - handwritten.KNOWN_STEP_FIELDS, drift_message(
        "ci-infra's Step model declares fields we do not model: "
        f"{sorted(fields - handwritten.KNOWN_STEP_FIELDS)}.",
        "Preflight force-selects every step carrying an unmodelled field, and "
        "a forced step is not droppable, so it costs CI time on every PR until "
        "it is listed.",
        f"add them to KNOWN_STEP_FIELDS in {HW}",
        "if the field changes what the step runs: also teach Step in "
        "ci_selector/codemap/pipeline/step.py to read it",
    )


# ----------------------------------------------------------------- behaviour


@pytest.mark.drift
def test_our_declaration_matching_behaves_like_ci_infras():
    """A whole declaration against a whole diff, so the include/exclude split
    and the any-file-matches rule are covered, not just one pair.

    Not a copy check. `deps_match` is now upstream's own function, but
    `step_declares` is ours: it answers per path and returns a ranked winner,
    which upstream has no equivalent for. This is the only thing checking that
    our per-path predicate still composes to upstream's whole-diff answer.
    """
    from ci_selector.codemap.claim import step_declares

    theirs = buildkite_step._source_file_dependencies_match
    for deps in DEP_SETS:
        for size in range(3):
            for diff in itertools.combinations(PATHS, size):
                mine = any(step_declares(deps, p) for p in diff)
                assert theirs(deps, list(diff)) == mine, drift_message(
                    f"Declaration matching disagrees with ci-infra on "
                    f"deps={deps}, diff={list(diff)}: they say "
                    f"{theirs(deps, list(diff))}, we say {mine}.",
                    "A `!` entry carves a subtree out of a broader positive "
                    "one, and the decision is per step rather than per entry. "
                    "Getting it wrong changes which steps a diff selects.",
                    "read _source_file_dependencies_match in the generator's "
                    "buildkite_step.py and make step_declares in claim.py "
                    "agree with it",
                )


@pytest.mark.drift
def test_our_docs_only_behaves_like_ci_infras_apart_from_empty_paths():
    """One deliberate difference, pinned so it cannot grow.

    ci-infra skips an empty path and can therefore call a diff of nothing but
    empty strings docs-only, which emits no steps at all. We call it not
    docs-only and run the ordinary rules. Ours is the conservative side and
    `git diff --name-only` never emits an empty path, so we keep our own
    predicate rather than importing theirs; the assertions below fail if the
    difference ever spreads beyond that case or flips direction.
    """
    from ci_selector.codemap.claim import docs_only

    theirs = generator_mod.is_docs_only_change
    diverged = []
    for size in range(4):
        for diff in itertools.combinations(PATHS, size):
            paths = list(diff)
            if theirs(paths) != docs_only(paths):
                diverged.append(paths)
    assert all("" in d for d in diverged), drift_message(
        "docs_only now disagrees with ci-infra on a diff with no empty path: "
        f"{[d for d in diverged if '' not in d][:5]}",
        "A whole-diff docs-only answer emits nothing at all, so this predicate "
        "is the one place we can take CI to zero steps. It is the most "
        "expensive thing in the tool to get wrong.",
        "read is_docs_only_change in the generator's pipeline_generator.py "
        "and make docs_only in claim.py agree",
    )
    assert all(theirs(d) and not docs_only(d) for d in diverged), drift_message(
        "The empty-path difference with ci-infra has flipped direction: we now "
        "call something docs-only that they do not.",
        "That direction emits no steps where CI would run some, which is "
        "under-selection on the cheapest possible diff.",
        "make docs_only in claim.py at least as conservative as ci-infra's "
        "is_docs_only_change",
    )


@pytest.mark.drift
def test_our_replica_behaves_like_the_generators_own_rule():
    """The `_step_should_run` replica, against upstream's real code.

    Upstream needs a few things we have to supply: its global config, a step
    type, and its AMD-device test, which we substitute with our own
    `family_of_device`. So this checks the branch structure and its order, not
    those substituted parts, which have their own guards. The three branches we
    deliberately do not model (NOAUTO, only_step_keys, nightly) are pinned off
    here rather than left to chance.

    Still an exec of the generator's source rather than a direct call, because
    the substitutions are the point: calling `_step_should_run` itself would
    also test its `is_amd_gpu_device` and its global config, which are not what
    our replica claims to reproduce.
    """
    import inspect
    import os
    import textwrap
    from types import SimpleNamespace
    from typing import List

    from ci_selector.codemap.hardware import family_of_device
    from ci_selector.handwritten import (
        AMD_ALWAYS_RUN_STEP_KEYS,
        AMD_NATIVE_RUNTIME_SOURCE_DEPENDENCIES,
        IMAGE_BUILD_KEY_PREFIX,
    )
    from ci_selector.validate.generator_replica import step_should_run

    config = {"only_step_keys": None, "nightly": "0", "run_all": False}
    namespace = {
        "os": os,
        "List": List,
        "Step": SimpleNamespace,
        "get_global_config": lambda: config,
        "AMD_ALWAYS_RUN_STEP_KEYS": AMD_ALWAYS_RUN_STEP_KEYS,
        "AMD_NATIVE_RUNTIME_SOURCE_DEPENDENCIES": list(
            AMD_NATIVE_RUNTIME_SOURCE_DEPENDENCIES
        ),
        "is_amd_gpu_device": lambda d: family_of_device(d) == "amd",
        "_source_file_dependencies_match": (
            buildkite_step._source_file_dependencies_match
        ),
    }
    source = textwrap.dedent(inspect.getsource(buildkite_step._step_should_run))
    exec(source, namespace)  # noqa: S102
    theirs = namespace["_step_should_run"]

    amd_script = AMD_NATIVE_RUNTIME_SOURCE_DEPENDENCIES[0]
    keys = [None, "lora", "image-build", "image-build-amd", *AMD_ALWAYS_RUN_STEP_KEYS]
    devices = [None, "h100", "mi300_1", "cpu-small"]
    diffs = [[], ["vllm/x.py"], ["tests/t.py"], [amd_script], ["docs/a.md"]]
    checked = 0
    for key, device, dind, optional, deps, diff, run_all in itertools.product(
        keys, devices, (False, True), (False, True), DEP_SETS, diffs, (False, True)
    ):
        config["run_all"] = run_all
        always = bool(key) and (
            key.startswith(IMAGE_BUILD_KEY_PREFIX) or key in AMD_ALWAYS_RUN_STEP_KEYS
        )
        common = dict(
            key=key,
            optional=optional,
            device=device,
            dind=dind,
            source_file_dependencies=deps,
        )
        mine = step_should_run(
            SimpleNamespace(always_runs=always, mirror_hw=None, **common), diff, run_all
        )
        checked += 1
        assert theirs(SimpleNamespace(**common), diff) == mine, drift_message(
            "Our _step_should_run replica disagrees with ci-infra's on "
            f"key={key!r}, device={device!r}, dind={dind}, optional={optional}, "
            f"deps={deps}, diff={diff}, run_all={run_all}.",
            "The replica is the baseline every recall and cost figure is "
            "measured against. If it stops describing the real generator, the "
            "numbers describe a CI that does not exist.",
            "read _step_should_run in the generator's buildkite_step.py and "
            "make step_should_run in ci_selector/validate/generator_replica.py "
            "agree with it",
        )
    assert checked > 1000, "the input space collapsed; this proves nothing"
