# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""`RowKeys.resolve` reads only the pipeline yaml at the table's commit, and
must give the same answer as resolving against the whole `RepoState` there."""

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


def test_resolve_reads_the_table_commit_not_the_checkout(pipeline_repo):
    """Labels move, so the yaml must come from the commit the rows were
    recorded at, not from whatever the checkout holds."""
    repo, recorded, later = pipeline_repo
    table = _table("keyed")
    at_recorded = RowKeys.resolve(table, repo.root, recorded)
    at_later = RowKeys.resolve(table, repo.root, later)
    assert "vllm_ci:later" not in at_recorded.steps
    assert "vllm_ci:later" in at_later.steps


def test_a_step_renamed_after_the_table_is_filed_under_its_new_id(
    tmp_path, monkeypatch
):
    """Against real yaml: the table's commit spells a step `old-key`, the
    base renames it. `_steps_at` maps each identity to the base steps holding
    it (twins included), and `restrict_to` files the step under its base id
    while its row stays `old-key`."""
    from ci_selector.decide import _steps_at

    monkeypatch.setattr(worktree, "WORKTREE_CACHE", tmp_path / "cache")
    worktree.clear_state_cache()
    (tmp_path / "repo").mkdir()
    repo = Repo(tmp_path / "repo")
    twins = (
        "  - label: Twin A\n    commands: [pytest tests/t.py]\n"
        "  - label: Twin B\n    commands: [pytest tests/t.py]\n"
    )
    renamed = "  - label: Renamed\n    key: {key}\n    commands: [pytest tests/r.py]\n"
    repo.write(".buildkite/ci_config.yaml", CI_CONFIG)
    repo.write(
        ".buildkite/test_areas/area.yaml",
        "steps:\n" + renamed.format(key="old-key") + twins,
    )
    recorded = repo.commit("recorded")
    repo.write(
        ".buildkite/test_areas/area.yaml",
        "steps:\n" + renamed.format(key="new-key") + twins,
    )
    base = repo.commit("rename")

    keys = RowKeys.resolve(_table("old-key"), repo.root, recorded)
    ids, holders = _steps_at(repo.root, base)
    twin_identity = keys.steps["vllm_ci:Twin A"].identity
    assert [s.step_id for s in holders[twin_identity]] == [
        "vllm_ci:Twin A",
        "vllm_ci:Twin B",
    ]
    assert "vllm_ci:old-key" not in ids

    keys.restrict_to(ids, holders)
    assert keys.steps["vllm_ci:new-key"].buildkite_key == "new-key"
    assert keys.key_for("vllm_ci:new-key") == "old-key"
    assert "vllm_ci:new-key" in keys.candidates()
