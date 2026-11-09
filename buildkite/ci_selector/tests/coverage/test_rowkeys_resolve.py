# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""`RowKeys.resolve` reads the pipeline yaml at the table's commit, and only
that.

It used to build a whole `RepoState` there (the import graph of the full vLLM
tree), which was half of every CI run's selector time, for the one field it
reads: the pipelines.
"""

from __future__ import annotations

import pytest
from ci_selector.codemap import worktree
from ci_selector.coverage.rules import RowKeys
from ci_selector.coverage.table import Table

from .helpers import Repo

CI_CONFIG = """\
name: vllm_ci
job_dirs:
  - .buildkite/test_areas
"""

JOBS = """\
group: Area
steps:
  - label: Keyed Step
    key: keyed
    commands:
      - pytest -v -s tests/test_a.py
  - label: Label Only
    commands:
      - pytest -v -s tests/test_b.py
  - label: Nightly
    key: nightly
    optional: true
    commands:
      - pytest -v -s tests/test_c.py
"""


@pytest.fixture
def pipeline_repo(tmp_path, monkeypatch) -> tuple[Repo, str, str]:
    """A repo whose pipeline gains a step after the commit the table recorded."""
    monkeypatch.setattr(worktree, "WORKTREE_CACHE", tmp_path / "cache")
    worktree.clear_state_cache()
    (tmp_path / "repo").mkdir()
    repo = Repo(tmp_path / "repo")
    repo.write(".buildkite/ci_config.yaml", CI_CONFIG)
    repo.write(".buildkite/test_areas/area.yaml", JOBS)
    recorded = repo.commit("recorded")
    repo.write(
        ".buildkite/test_areas/later.yaml",
        "steps:\n  - label: Later\n    key: later\n    commands: [pytest tests/l.py]\n",
    )
    later = repo.commit("later")
    return repo, recorded, later


def _table(*row_keys: str) -> Table:
    return Table({key: object() for key in row_keys})


@pytest.fixture
def no_state(monkeypatch):
    """Fail the test if anything builds a RepoState or an import graph."""

    def refuse(*args, **kwargs):
        raise AssertionError("resolve built a RepoState; it needs only the yaml")

    monkeypatch.setattr(worktree, "state_for", refuse)
    monkeypatch.setattr("ci_selector.codemap.state.RepoState.build", refuse)
    monkeypatch.setattr("ci_selector.codemap.state.build_full_graph", refuse)


def test_resolve_reads_only_the_pipeline_yaml(pipeline_repo, no_state):
    repo, recorded, _ = pipeline_repo
    keys = RowKeys.resolve(_table("keyed", "nightly"), repo.root, recorded)
    assert keys.owners == {"vllm_ci"}
    assert keys.match_rate == {"vllm_ci": 1.0}


def test_resolve_answers_as_the_full_state_did(pipeline_repo):
    """Owners, rates, spellings and the step objects: the same answer as
    resolving against the whole `RepoState` at that commit, which is what
    `resolve` used to build."""
    repo, recorded, _ = pipeline_repo
    table = _table("keyed", "Label Only", "a-row-no-step-spells")

    state = worktree.state_for(repo.root, recorded)
    expected = RowKeys.resolve_from_pipelines(set(table._rows), state.pipelines)
    got = RowKeys.resolve(table, repo.root, recorded)
    assert got.owners == expected.owners
    assert got.match_rate == expected.match_rate
    assert got.spellings == expected.spellings
    assert got.steps.keys() == expected.steps.keys()
    assert all(got.steps[k] == expected.steps[k] for k in got.steps)
    assert got.candidates() == expected.candidates()


def test_resolve_reads_the_table_commit_not_the_checkout(pipeline_repo, no_state):
    """Labels move, so the yaml must come from the commit the rows were
    recorded at, not from whatever the checkout holds."""
    repo, recorded, later = pipeline_repo
    table = _table("keyed")
    at_recorded = RowKeys.resolve(table, repo.root, recorded)
    at_later = RowKeys.resolve(table, repo.root, later)
    assert "vllm_ci:later" not in at_recorded.steps
    assert "vllm_ci:later" in at_later.steps


def test_no_rows_means_no_rate(pipeline_repo, no_state):
    repo, recorded, _ = pipeline_repo
    keys = RowKeys.resolve(_table(), repo.root, recorded)
    assert keys.match_rate == {"vllm_ci": 0.0}
