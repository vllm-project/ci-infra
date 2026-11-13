import subprocess

import pytest

import buildkite_step
import step_definitions
from buildkite_step import _get_amd_mirror_effective_step, _step_should_run
from step import Step, read_steps_from_job_dir

YAML = ".buildkite/test_areas/models.yaml"

HEADER = """\
group: Models
depends_on:
  - image-build
steps:
"""
KIMI = """\
- label: ":nvidia: (H100) Kimi"
  key: kimi
  device: h100
  source_file_dependencies:
  - vllm/models/kimi/
  commands:
  - pytest models/kimi
  mirror:
    amd:
      label: ":amd: (MI355) Kimi"
      device: mi355_1
      commands:
      - pytest models/kimi
"""
DECODE = """\
- label: ":amd: (MI355) Kimi Decode"
  key: kimi-decode-amd
  device: mi355_8
  depends_on:
  - image-build-amd
  source_file_dependencies:
  - vllm/models/kimi/amd/
  commands:
  - pytest models/kimi/amd
"""
BASE = HEADER + KIMI + DECODE


def _edit(text, old, new):
    assert old in text
    return text.replace(old, new, 1)


def test_unchanged_file_redefines_nothing():
    assert step_definitions.changed_step_keys(BASE, BASE) == frozenset()


def test_reordering_steps_redefines_nothing():
    reordered = HEADER + DECODE + KIMI
    assert step_definitions.changed_step_keys(BASE, reordered) == frozenset()


def test_a_parent_edit_redefines_the_step_and_its_amd_mirror():
    head = _edit(
        BASE,
        "  - pytest models/kimi\n  mirror:",
        "  - pytest -x models/kimi\n  mirror:",
    )
    assert step_definitions.changed_step_keys(BASE, head) == {"kimi", "amd-kimi"}


def test_a_mirror_edit_redefines_only_the_mirror():
    head = _edit(BASE, "      device: mi355_1\n", "      device: mi355_2\n")
    assert step_definitions.changed_step_keys(BASE, head) == {"amd-kimi"}


def test_a_step_without_an_amd_mirror_redefines_only_itself():
    head = _edit(
        BASE, "  - pytest models/kimi/amd\n", "  - pytest -x models/kimi/amd\n"
    )
    assert step_definitions.changed_step_keys(BASE, head) == {"kimi-decode-amd"}


def test_removing_the_amd_mirror_redefines_no_mirror():
    head = _edit(
        BASE,
        "  mirror:\n    amd:",
        "  mirror:\n    torch_nightly:",
    )
    assert step_definitions.changed_step_keys(BASE, head) == frozenset()


def test_a_non_amd_mirror_edit_leaves_the_amd_mirror():
    head = _edit(
        BASE,
        "  mirror:\n    amd:",
        "  mirror:\n    torch_nightly:\n      label: tn\n    amd:",
    )
    assert step_definitions.changed_step_keys(BASE, head) == frozenset()


def test_an_added_step_is_redefined_and_a_removed_one_is_not():
    added = BASE + "- label: New\n  key: new-step\n  commands:\n  - pytest new\n"
    assert step_definitions.changed_step_keys(BASE, added) == {"new-step"}
    assert step_definitions.changed_step_keys(added, BASE) == frozenset()


@pytest.mark.parametrize(
    "head",
    [
        _edit(BASE, "group: Models\n", "group: Models - Basic\n"),
        _edit(BASE, "  - image-build\nsteps:", "  - image-build-amd\nsteps:"),
        _edit(BASE, "  key: kimi-decode-amd\n", ""),
        _edit(BASE, "  key: kimi-decode-amd\n", "  key: kimi\n"),
        "steps: [unclosed\n",
    ],
    ids=["group", "group-depends-on", "missing-key", "duplicate-key", "unparsable"],
)
def test_an_edit_outside_single_steps_is_not_pinned(head):
    assert step_definitions.changed_step_keys(BASE, head) is None


def test_a_new_file_is_not_pinned():
    assert step_definitions.changed_step_keys(None, BASE) is None


def test_changed_step_keys_in_file_reads_the_base_from_git(tmp_path, monkeypatch):
    path = tmp_path / YAML
    path.parent.mkdir(parents=True)
    path.write_text(BASE)
    git = ["git", "-c", "user.email=ci@example.com", "-c", "user.name=ci"]
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(git + ["add", "."], cwd=tmp_path, check=True)
    subprocess.run(git + ["commit", "-qm", "base"], cwd=tmp_path, check=True)
    path.write_text(
        _edit(BASE, "  - pytest models/kimi/amd\n", "  - pytest -x models/kimi/amd\n")
    )
    monkeypatch.chdir(tmp_path)
    step_definitions.changed_step_keys_in_file.cache_clear()

    assert step_definitions.changed_step_keys_in_file(YAML, "HEAD") == {
        "kimi-decode-amd"
    }
    assert step_definitions.changed_step_keys_in_file(YAML, "missing-ref") is None
    assert step_definitions.changed_step_keys_in_file(YAML, None) is None


@pytest.fixture
def steps(tmp_path, monkeypatch, fake_global_config):
    path = tmp_path / YAML
    path.parent.mkdir(parents=True)
    path.write_text(BASE)
    monkeypatch.chdir(tmp_path)
    loaded = {s.key: s for s in read_steps_from_job_dir(".buildkite/test_areas")}
    parent = loaded["kimi"]
    loaded["amd-kimi"] = _get_amd_mirror_effective_step(parent, parent.mirror["amd"])
    return loaded


def _redefined(monkeypatch, keys):
    monkeypatch.setattr(
        step_definitions, "changed_step_keys_in_file", lambda path, base: keys
    )


def test_read_steps_records_each_steps_file(steps):
    assert {s.source_file for s in steps.values()} == {YAML}
    assert all(YAML in s.source_file_dependencies for s in steps.values())
    assert "source_file" not in steps["kimi"].model_dump()


def test_amd_steps_skip_an_edit_to_another_step_in_their_file(steps, monkeypatch):
    _redefined(monkeypatch, frozenset({"kimi"}))

    assert _step_should_run(steps["kimi"], [YAML])
    assert not _step_should_run(steps["kimi-decode-amd"], [YAML])
    assert not _step_should_run(steps["amd-kimi"], [YAML])


def test_amd_steps_run_when_their_own_definition_changes(steps, monkeypatch):
    _redefined(monkeypatch, frozenset({"kimi-decode-amd", "amd-kimi"}))

    assert _step_should_run(steps["kimi-decode-amd"], [YAML])
    assert _step_should_run(steps["amd-kimi"], [YAML])


def test_amd_steps_run_on_an_edit_that_is_not_pinned(steps, monkeypatch):
    _redefined(monkeypatch, None)

    assert _step_should_run(steps["kimi-decode-amd"], [YAML])
    assert _step_should_run(steps["amd-kimi"], [YAML])


def test_amd_steps_still_run_on_their_other_dependencies(steps, monkeypatch):
    _redefined(monkeypatch, frozenset())

    assert _step_should_run(
        steps["kimi-decode-amd"], [YAML, "vllm/models/kimi/amd/x.py"]
    )
    assert _step_should_run(steps["amd-kimi"], ["vllm/models/kimi/model.py"])
    assert not _step_should_run(steps["kimi-decode-amd"], ["vllm/models/kimi/model.py"])


def _load(tmp_path, monkeypatch, text):
    path = tmp_path / YAML
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    monkeypatch.chdir(tmp_path)
    loaded = {s.key: s for s in read_steps_from_job_dir(".buildkite/test_areas")}
    for parent in list(loaded.values()):
        if parent.mirror and "amd" in parent.mirror:
            loaded[f"amd-{parent.key}"] = _get_amd_mirror_effective_step(
                parent, parent.mirror["amd"]
            )
    return loaded


def test_a_step_that_lists_its_own_yaml_runs_on_any_edit_to_it(
    tmp_path, monkeypatch, fake_global_config
):
    declared = _edit(
        BASE,
        "  - vllm/models/kimi/amd/\n",
        f"  - vllm/models/kimi/amd/\n  - {YAML}\n",
    )
    declared = _edit(
        declared,
        "      device: mi355_1\n",
        f"      device: mi355_1\n      source_file_dependencies:\n      - {YAML}\n",
    )
    loaded = _load(tmp_path, monkeypatch, declared)
    _redefined(monkeypatch, frozenset())

    assert loaded["kimi-decode-amd"].source_file_declared
    assert loaded["amd-kimi"].source_file_declared
    assert _step_should_run(loaded["kimi-decode-amd"], [YAML])
    assert _step_should_run(loaded["amd-kimi"], [YAML])


def test_an_amd_mirror_inherits_its_parents_own_yaml_listing(
    tmp_path, monkeypatch, fake_global_config
):
    declared = _edit(
        BASE, "  - vllm/models/kimi/\n", f"  - vllm/models/kimi/\n  - {YAML}\n"
    )
    loaded = _load(tmp_path, monkeypatch, declared)
    _redefined(monkeypatch, frozenset())

    assert not loaded["kimi-decode-amd"].source_file_declared
    assert _step_should_run(loaded["amd-kimi"], [YAML])
    assert not _step_should_run(loaded["kimi-decode-amd"], [YAML])


def test_an_edit_to_one_step_runs_only_it_end_to_end(
    tmp_path, monkeypatch, fake_global_config
):
    path = tmp_path / YAML
    path.parent.mkdir(parents=True)
    path.write_text(BASE)
    git = ["git", "-c", "user.email=ci@example.com", "-c", "user.name=ci"]
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(git + ["add", "."], cwd=tmp_path, check=True)
    subprocess.run(git + ["commit", "-qm", "base"], cwd=tmp_path, check=True)
    path.write_text(
        _edit(BASE, "  - pytest models/kimi/amd\n", "  - pytest -x models/kimi/amd\n")
    )
    monkeypatch.chdir(tmp_path)
    fake_global_config["merge_base_commit"] = "HEAD"
    step_definitions.changed_step_keys_in_file.cache_clear()
    loaded = _load(tmp_path, monkeypatch, path.read_text())

    assert _step_should_run(loaded["kimi-decode-amd"], [YAML])
    assert not _step_should_run(loaded["amd-kimi"], [YAML])
    assert _step_should_run(loaded["kimi"], [YAML])
    step_definitions.changed_step_keys_in_file.cache_clear()


def test_the_bootstrap_log_names_what_an_edit_redefines(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    step_definitions.changed_step_keys_in_file.cache_clear()
    assert step_definitions.changed_step_keys_in_file(YAML, None) is None
    assert "not pinned to steps" in capsys.readouterr().out
    step_definitions.changed_step_keys_in_file.cache_clear()


def _optional(step):
    return step.model_copy(update={"optional": True})


def test_an_optional_amd_step_runs_when_its_own_definition_changes(steps, monkeypatch):
    _redefined(monkeypatch, frozenset({"kimi-decode-amd", "amd-kimi"}))

    assert _step_should_run(_optional(steps["kimi-decode-amd"]), [YAML])
    assert _step_should_run(_optional(steps["amd-kimi"]), [YAML])


def test_an_optional_amd_step_waits_when_another_step_changes(steps, monkeypatch):
    _redefined(monkeypatch, frozenset({"kimi"}))

    assert not _step_should_run(_optional(steps["kimi-decode-amd"]), [YAML])


def test_an_edit_that_is_not_pinned_runs_no_optional_step(steps, monkeypatch):
    _redefined(monkeypatch, None)

    assert _step_should_run(steps["kimi-decode-amd"], [YAML])
    assert not _step_should_run(_optional(steps["kimi-decode-amd"]), [YAML])


def test_an_optional_amd_step_ignores_its_other_dependencies(steps, monkeypatch):
    _redefined(monkeypatch, frozenset({"kimi-decode-amd"}))

    assert not _step_should_run(
        _optional(steps["kimi-decode-amd"]), ["vllm/models/kimi/amd/x.py"]
    )


def test_a_redefined_optional_non_amd_step_is_left_to_the_selector(steps, monkeypatch):
    _redefined(monkeypatch, frozenset({"kimi", "amd-kimi"}))

    assert not _step_should_run(_optional(steps["kimi"]), [YAML])


def test_definition_base_ref_follows_the_diff(fake_global_config):
    fake_global_config["merge_base_commit"] = "abc"
    assert buildkite_step._definition_base_ref() == "abc"
    fake_global_config["branch"] = "main"
    assert buildkite_step._definition_base_ref() == "HEAD~1"


def test_a_step_without_its_file_as_a_dependency_is_unchanged(fake_global_config):
    step = Step(
        label=":amd: (MI355) Standalone",
        key="standalone-amd",
        device="mi355_1",
        source_file=YAML,
        source_file_dependencies=["vllm/"],
        commands=["pytest"],
    )
    assert not _step_should_run(step, [YAML])
    assert _step_should_run(step, ["vllm/x.py"])
