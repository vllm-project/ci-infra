# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""A job file's edit selects the steps it adds or changes, named as the head
spells them, since CI generates the PR's pipeline from its head.

vllm#52928 added replayssm-e2e to engine.yaml, the only job running its new
test. The base pipeline had no such step, so nothing could select or emit it,
while all 14 steps the file already defined were held instead.
"""

from __future__ import annotations

import dataclasses
import subprocess
from pathlib import Path

import pytest
from ci_selector.codemap import worktree
from ci_selector.codemap.classify import select
from ci_selector.codemap.guards import PreflightReport
from ci_selector.codemap.pipeline.buildkite import edited_steps
from ci_selector.codemap.step_keys import emit
from ci_selector.gitdiff import changed_paths, diff_files

PATH = ".buildkite/test_areas/engine.yaml"

BASE = """\
group: Engine
depends_on:
  - image-build
steps:
- label: Engine
  key: engine
  device: h100
  source_file_dependencies:
  - vllm/engine/
  commands:
  - pytest -v -s engine
  mirror:
    amd:
      device: mi300_1
      commands:
      - pytest -v -s engine/amd
- label: Core
  key: core
  source_file_dependencies:
  - vllm/core/
  commands:
  - pytest -v -s core
  mirror:
    amd:
      device: mi250_1
      source_file_dependencies:
      - vllm/platforms/rocm.py
- label: Keyless Step
  commands:
  - pytest -v -s keyless
"""

NEW_STEP = """\
- label: ReplaySSM E2E
  key: replayssm-e2e
  device: h100
  source_file_dependencies:
  - tests/v1/e2e/test_replayssm_decode.py
  commands:
  - pytest -v -s v1/e2e/test_replayssm_decode.py
"""


def _edit(old: str, new: str, text: str = BASE) -> str:
    assert old in text, old
    return text.replace(old, new)


def _edited(after: str, before: str = BASE):
    return edited_steps(before, after, PATH, "vllm_ci")


def test_an_added_step_is_the_only_one_edited():
    assert _edited(BASE + NEW_STEP) == {"vllm_ci:replayssm-e2e"}


def test_dependency_and_label_edits_change_no_step():
    """Only today's rules read `source_file_dependencies`, and a label only
    names the job. vllm#54025 added one dependency line to seven job files
    and held every step they define."""
    after = _edit("  - vllm/engine/\n", "  - vllm/engine/\n  - vllm/v1/engine/\n")
    after = _edit("- label: Engine\n", "- label: Engine (H100)\n", after)
    after = _edit(
        "      - vllm/platforms/rocm.py\n", "      - vllm/platforms/\n", after
    )
    assert _edited(after) == frozenset()


def test_a_changed_command_selects_the_step_and_the_mirrors_inheriting_it():
    after = _edit("  - pytest -v -s core\n", "  - pytest -v -s core --ignore=core/x\n")
    assert _edited(after) == {"vllm_ci:core", "vllm_ci:core-amd:amd"}


def test_a_parent_edit_skips_a_mirror_that_sets_the_key_itself():
    """vllm#54597 changed the shard script of kernels-b200, whose AMD mirror
    runs commands of its own."""
    after = _edit("  - pytest -v -s engine\n", "  - pytest -v -s engine -x\n")
    assert _edited(after) == {"vllm_ci:engine"}


def test_a_mirror_block_edit_selects_only_that_mirror():
    after = _edit("      device: mi250_1\n", "      device: mi300_1\n")
    assert _edited(after) == {"vllm_ci:core-amd:amd"}


def test_a_key_the_step_does_not_model_still_counts():
    """`retry` never reaches a Step field, and still changes what the job
    does. The mirror does not set it, so the mirror counts too."""
    after = _edit("  device: h100\n", "  device: h100\n  retry:\n    automatic: true\n")
    assert _edited(after) == {"vllm_ci:engine", "vllm_ci:engine-amd:amd"}


def test_a_keyless_step_renamed_is_a_new_step():
    """Without a key the label is the id, and the generator publishes the new
    one, so the step has to be named the head's way."""
    after = _edit("- label: Keyless Step\n", "- label: Keyless Step Renamed\n")
    assert _edited(after) == {"vllm_ci:Keyless Step Renamed"}


@pytest.mark.parametrize(
    "after",
    [
        _edit("  - image-build\n", "  - image-build\n  - other-build\n"),
        _edit("group: Engine\n", "group: Engine Tests\n"),
        BASE + "  - [unbalanced\n",
        "not: a job file\n",
        _edit("key: core\n", "key: engine\n"),
    ],
    ids=["file-depends-on", "group", "unparsable", "not-a-job-file", "shared-id"],
)
def test_what_cannot_be_paired_step_by_step_is_the_whole_file(after):
    assert _edited(after) is None


def test_an_added_file_adds_every_step_and_a_deleted_one_none():
    every = {
        "vllm_ci:engine",
        "vllm_ci:engine-amd:amd",
        "vllm_ci:core",
        "vllm_ci:core-amd:amd",
        "vllm_ci:Keyless Step",
    }
    assert _edited(BASE, before=None) == every
    assert edited_steps(BASE, None, PATH, "vllm_ci") == frozenset()


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def pipeline_repo(tmp_path):
    """A repo holding nothing but a pipeline: BASE, then a head adding a step
    and editing another's dependencies."""
    repo = tmp_path / "vllm"
    (repo / ".buildkite" / "test_areas").mkdir(parents=True)
    (repo / ".buildkite" / "ci_config.yaml").write_text(
        "name: vllm_ci\njob_dirs:\n  - .buildkite/test_areas\n"
        "run_all_patterns: []\nrun_all_exclude_patterns: []\n"
    )
    (repo / PATH).write_text(BASE)
    _git(repo, "init", "-q")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / PATH).write_text(
        _edit("  - vllm/core/\n", "  - vllm/core/\n  - vllm/v1/core/\n") + NEW_STEP
    )
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "head")
    worktree.clear_state_cache()
    yield repo, base, _git(repo, "rev-parse", "HEAD")
    worktree.clear_state_cache()


def _selection(state, repo, base, head):
    # A tree with no vLLM in it fails half of preflight's checks, which would
    # escalate everything; the claim under test is not about any of them.
    state = dataclasses.replace(state, preflight=PreflightReport())
    paths = changed_paths(diff_files(repo, base, head))
    return select(state, paths, base=base, head=head)


def test_a_step_the_pr_adds_is_selected_and_emitted(pipeline_repo):
    repo, base, head = pipeline_repo
    paths = changed_paths(diff_files(repo, base, head))
    state = worktree.with_head_steps(worktree.state_for(repo, base), repo, head, paths)
    sel = _selection(state, repo, base, head)
    assert set(sel.selected) == {"vllm_ci:replayssm-e2e"}
    emission = emit(state, sel)
    assert not emission.omit, emission.reason
    assert emission.keys == ["replayssm-e2e"]


def test_without_the_head_steps_the_file_is_claimed_whole(pipeline_repo):
    """The base cannot name a step the PR added, so the claim falls back to
    every step the base defines rather than to a set nothing can emit."""
    repo, base, head = pipeline_repo
    sel = _selection(worktree.state_for(repo, base), repo, base, head)
    assert set(sel.selected) == {
        "vllm_ci:engine",
        "vllm_ci:engine-amd:amd",
        "vllm_ci:core",
        "vllm_ci:core-amd:amd",
        "vllm_ci:Keyless Step",
    }


def test_a_diff_without_job_files_keeps_the_base_state(pipeline_repo):
    repo, base, head = pipeline_repo
    state = worktree.state_for(repo, base)
    assert worktree.with_head_steps(state, repo, head, ["vllm/x.py"]) is state
