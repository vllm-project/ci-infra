# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
import contextlib
import fcntl
import os
import subprocess
import sys
from pathlib import Path

import pytest

VLLM_REMOTE = "https://github.com/vllm-project/vllm"
PIN_FILE = Path(__file__).resolve().parents[1] / "VLLM_PIN"

# A clone this suite owns, so reading it cannot disturb anyone's own checkout.
CLONE = Path(
    os.environ.get("CI_SELECTOR_VLLM_CLONE")
    or Path.home() / ".cache" / "vllm-ci-selector" / "vllm"
)


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


def _commit(repo: Path, ref: str) -> str | None:
    out = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "rev-parse",
            "--verify",
            "--quiet",
            f"{ref}^{{commit}}",
        ],
        capture_output=True,
        text=True,
    )
    return out.stdout.strip() or None


@contextlib.contextmanager
def _lock(path: Path):
    """One clone, shared by every run on this machine, so only one of them may
    be moving it at a time."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _clone_at(ref: str) -> tuple[Path, str]:
    """A vLLM checkout at `ref`, cloned once and kept.

    Blobless and with full history, the same way the workflow clones it: the
    suite builds worktrees and asks for merge bases, so a shallow one will not
    do.
    """
    with _lock(CLONE.parent / ".vllm-clone.lock"):
        if not (CLONE / ".git").is_dir():
            print(
                f"cloning vLLM into {CLONE}, once, this takes a few minutes",
                file=sys.stderr,
                flush=True,
            )
            CLONE.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["git", "clone", "--filter=blob:none", "--", VLLM_REMOTE, str(CLONE)],
                check=True,
            )
        want = _commit(CLONE, ref)
        if want is None:
            subprocess.run(
                ["git", "-C", str(CLONE), "fetch", "--filter=blob:none", "origin", ref],
                check=True,
            )
            want = _commit(CLONE, "FETCH_HEAD")
        if want is None:
            raise RuntimeError(f"{ref} is not a commit in {VLLM_REMOTE}")
        if _git(CLONE, "rev-parse", "HEAD") != want:
            _git(CLONE, "checkout", "--detach", "--force", want)
    return CLONE, want


def _vllm_repo() -> tuple[Path, str, str]:
    """The vLLM checkout to analyse, and where it came from.

    The pin by default, in a clone of our own, so a result does not depend on
    what anyone's checkout happens to be sitting at. VLLM_REPO reads a checkout
    as it is instead, which is how drift against a newer vLLM gets found, and
    VLLM_REF moves our clone somewhere else.
    """
    raw = os.environ.get("VLLM_REPO")
    if raw:
        repo = Path(raw).expanduser().resolve()
        if not (repo / ".buildkite").is_dir():
            raise RuntimeError(
                f"VLLM_REPO={repo} does not look like a vLLM checkout: no "
                ".buildkite/ directory."
            )
        return repo, _git(repo, "rev-parse", "HEAD"), "VLLM_REPO"
    ref = os.environ.get("VLLM_REF")
    repo, head = _clone_at(ref or PIN_FILE.read_text().strip())
    return repo, head, "VLLM_REF" if ref else "VLLM_PIN"


REPO, HEAD, SOURCE = _vllm_repo()
# Some tests read the variable rather than this module, so they have to agree.
os.environ["VLLM_REPO"] = str(REPO)


def pytest_report_header(config):
    pin = PIN_FILE.read_text().strip()
    line = f"vllm: {REPO} at {HEAD[:12]} (from {SOURCE})"
    if HEAD != pin:
        line += f", not VLLM_PIN {pin[:12]}: a failure may be drift, not this code"
    return line


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
