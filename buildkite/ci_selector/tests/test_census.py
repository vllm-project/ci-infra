# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Guard against unmodelled files that trigger the whole pipeline.

Files that no CI job can test should select only the always-run floor, not run
everything. These tests read the current tree from git each run, so a new batch
of such files is caught, not just a regression of known ones. The only
maintained value is one ceiling integer.
"""

import subprocess
from pathlib import Path

import pytest
from ci_selector.codemap.classify import select
from helpers import drift_message

# Where new unmodelled files land. Not the whole tree: classifying every file
# is slow, and this scope is where accumulation happens.
SURFACE_PREFIXES = ("tools/", ".buildkite/")
# How many scoped files may legitimately run everything (build inputs, docker
# scripts, pipeline configs), with headroom before the guard fires.
RUNALL_CEILING = 20


def _tracked(repo: Path, *prefixes: str) -> list[str]:
    out = subprocess.check_output(
        ["git", "-C", str(repo), "ls-files", *prefixes], text=True
    ).split()
    return out


def _surface_files(repo: Path) -> list[str]:
    scoped = _tracked(repo, *SURFACE_PREFIXES)
    root = [f for f in _tracked(repo) if "/" not in f]
    return scoped + root


@pytest.mark.drift
def test_the_unmodelled_surface_stays_bounded(state):
    """Ceiling on how many scoped files run everything, read from the live
    tree. Catches a rule regression re-escalating files and a new batch of
    unmodelled ones accumulating."""
    files = _surface_files(state.repo)
    assert len(files) >= 200, drift_message(
        f"the scoped surface resolved to only {len(files)} files",
        "the guard is not seeing the tree it was built to watch",
        "SURFACE_PREFIXES in tests/test_census.py",
    )
    escalate = [f for f in files if select(state, [f]).run_all]
    assert len(escalate) <= RUNALL_CEILING, drift_message(
        f"{len(escalate)} scoped files run everything (ceiling "
        f"{RUNALL_CEILING}): {sorted(escalate)[:12]}",
        "each makes every PR touching it run the whole pipeline; a jump means "
        "a rule broke or an unmodelled class accumulated",
        "the inert and inheritance branches in codemap/classify.py",
    )


@pytest.mark.drift
def test_global_build_inputs_still_escalate(state):
    """The inert veto must never silence a global build input. CMakeLists.txt
    and pyproject.toml compile into every wheel, so running everything is the
    right answer for them."""
    for path in ("CMakeLists.txt", "pyproject.toml"):
        assert (state.repo / path).is_file(), f"{path} moved; update this guard"
        assert select(state, [path]).run_all, drift_message(
            f"{path} no longer triggers run-all",
            "a global build input was silenced; PRs changing it under-run",
            "the inert veto in codemap/classify.py (_reached_by_nothing)",
        )


# `test_our_own_package_selects_nothing_beyond_the_floor` was here. It walked
# vLLM's `tools/ci_selector/` and asserted our own files selected nothing beyond
# the always-run floor. That question stops existing once the tool lives in
# ci-infra: there is nothing of ours in the vLLM tree to ask it about, and the
# test would go red the day the old copy is removed. The behaviour it leaned on,
# the inert veto in `codemap/classify.py`, is covered by six tests in
# `test_classify.py` that use vLLM's own inert trees.
