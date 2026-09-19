# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
import os
from pathlib import Path

import pytest


def _vllm_repo() -> Path:
    """The vLLM checkout to analyse.

    Required, with no fallback. This suite lives in ci-infra and reads a tree in
    another repository, so there is nothing sensible to guess: walking up from
    here lands on the ci-infra root, which is not vLLM.
    """
    raw = os.environ.get("VLLM_REPO")
    if not raw:
        raise RuntimeError(
            "VLLM_REPO is not set. It must point at a vLLM checkout, for "
            "example `VLLM_REPO=/path/to/vllm pytest tests -q`. The pinned "
            "commit this suite is known green against is in VLLM_PIN."
        )
    repo = Path(raw).expanduser().resolve()
    if not (repo / ".buildkite").is_dir():
        raise RuntimeError(
            f"VLLM_REPO={repo} does not look like a vLLM checkout: no "
            ".buildkite/ directory."
        )
    return repo


REPO = _vllm_repo()


@pytest.fixture(autouse=True, scope="session")
def _isolate_worktree_cache(tmp_path_factory):
    """Keep the suite out of the real worktree cache.

    Autouse and session-scoped because the leak is indirect and nobody
    remembers to opt in: a test can stub `state_for` and still reach
    `worktree_at` through `decide()`, which is how a pytest temp repo ended up
    registered in the live cache, pinned by a dead process's claim and
    invisible until someone listed the directory.

    Session-scoped so the trees built here are still shared between tests, and
    torn down with the session rather than left for the next run to trip over.
    """
    import ci_selector.codemap.worktree as wt

    original = wt.WORKTREE_CACHE
    wt.WORKTREE_CACHE = tmp_path_factory.mktemp("worktree-cache")
    try:
        yield wt.WORKTREE_CACHE
    finally:
        wt.release_claims()
        wt.WORKTREE_CACHE = original


@pytest.fixture(autouse=True)
def _quiet_preflight(request):
    """Fail a `quiet_preflight` test with the preflight problem itself.

    A force-selected step joins every selection, so one unreadable command
    fails a dozen unrelated rules with a set diff naming a step they never
    mention. Autouse so it always runs before the select() call, and
    marker-gated so no other test builds a state for it.

    It does not subtract the forced steps from the selection: that would hide
    a real over-selection that happened to land on one.
    """
    if request.node.get_closest_marker("quiet_preflight") is None:
        return
    from helpers import preflight_drift

    state = request.getfixturevalue("state")
    if state.preflight.force_select:
        pytest.fail(preflight_drift(state), pytrace=False)


@pytest.fixture(scope="session")
def vllm_repo() -> Path:
    """The real vLLM checkout. Named so it cannot be confused with the
    throwaway `tmp_repo` in tests/coverage/: same word, opposite meaning,
    and a test that got the wrong one would pass against nothing."""
    assert (REPO / ".buildkite" / "ci_config.yaml").is_file(), (
        f"{REPO} is not a vLLM checkout (set VLLM_REPO)"
    )
    return REPO


@pytest.fixture(scope="session")
def state(vllm_repo):
    from ci_selector.codemap.state import RepoState

    return RepoState.build(vllm_repo)


@pytest.fixture(scope="session")
def full(state):
    """The FullGraph, shared. Building one costs ~19s and parses every file in
    the checkout, so a per-module build is over half the suite's runtime.
    Nothing in select or preflight mutates it. A test that needs a graph built
    at a specific commit, or that times a cold build, must build its own."""
    return state.full


@pytest.fixture
def declared_deps_on(monkeypatch):
    """Let the hand-written declared lists pick steps again, for tests of
    behaviour that only exists then: declarer-union rules, declared-deps
    routes, and the few reaches the derived default gives up."""
    monkeypatch.setenv("CI_SELECTOR_DECLARED_DEPS", "on")
