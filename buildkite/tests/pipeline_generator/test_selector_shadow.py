"""The selector's shadow step (VLLM_CI_SELECTOR_SHADOW).

Off, the generated pipeline is what it was. On, a PR build gets one soft-fail
step that waits on nothing and that nothing waits on, so the shadow can never
hold or fail a test; main gets none.
"""

from pathlib import Path

import buildkite_step
import pipeline_generator
import pytest
import recorder_switches
import yaml

pytestmark = pytest.mark.usefixtures("fake_global_config")

JOBS = Path(__file__).resolve().parent / "test_files" / "test_jobs"


def _generate(tmp_path, monkeypatch, config, branch):
    config.update(branch=branch, job_dirs=[str(JOBS)])
    monkeypatch.setattr(pipeline_generator, "get_global_config", lambda: config)
    gen = pipeline_generator.PipelineGenerator.__new__(
        pipeline_generator.PipelineGenerator
    )
    gen.output_file_path = str(tmp_path / "pipeline.yaml")
    gen.generate()
    return yaml.safe_load(Path(gen.output_file_path).read_text())["steps"]


def _command_steps(groups):
    return [s for g in groups for s in g["steps"] if "commands" in s]


def _shadow(groups):
    return [
        s
        for s in _command_steps(groups)
        if s["key"] == buildkite_step.SELECTOR_SHADOW_KEY
    ]


@pytest.mark.parametrize("value", [None, "0", "true"])
def test_off_unless_exactly_one(tmp_path, monkeypatch, fake_global_config, value):
    monkeypatch.delenv(recorder_switches.SELECTOR_SHADOW_ENV_VAR, raising=False)
    if value is not None:
        monkeypatch.setenv(recorder_switches.SELECTOR_SHADOW_ENV_VAR, value)
    assert not _shadow(_generate(tmp_path, monkeypatch, fake_global_config, "pr"))


def test_off_leaves_the_pipeline_unchanged(tmp_path, monkeypatch, fake_global_config):
    monkeypatch.delenv(recorder_switches.SELECTOR_SHADOW_ENV_VAR, raising=False)
    off = _generate(tmp_path, monkeypatch, fake_global_config, "pr")
    monkeypatch.setenv(recorder_switches.SELECTOR_SHADOW_ENV_VAR, "1")
    on = _generate(tmp_path, monkeypatch, fake_global_config, "pr")
    assert [g for g in on if g["group"] != buildkite_step.SELECTOR_SHADOW_GROUP] == off


def test_on_a_pr_build(tmp_path, monkeypatch, fake_global_config):
    monkeypatch.setenv(recorder_switches.SELECTOR_SHADOW_ENV_VAR, "1")
    monkeypatch.setenv("VLLM_CI_BRANCH", "some-branch")
    groups = _generate(tmp_path, monkeypatch, fake_global_config, "user:feature")
    (shadow,) = _shadow(groups)
    assert shadow["label"] == ":crystal_ball: CI selector (shadow)"
    assert shadow["agents"] == {"queue": "small_cpu_queue_premerge"}
    assert shadow["soft_fail"] is True
    assert 0 < shadow["timeout_in_minutes"] <= 30
    assert "depends_on" not in shadow
    assert "allow_dependency_failure" not in shadow
    (command,) = shadow["commands"]
    assert (
        "raw.githubusercontent.com/vllm-project/ci-infra/some-branch/"
        "buildkite/ci_selector/shadow/run.sh"
    ) in command
    assert shadow["env"] == {"VLLM_CI_BRANCH": "some-branch"}


def test_nothing_waits_on_it(tmp_path, monkeypatch, fake_global_config):
    monkeypatch.setenv(recorder_switches.SELECTOR_SHADOW_ENV_VAR, "1")
    # With pre-commit on, which rewrites depends_on across the pipeline.
    fake_global_config["pull_request"] = "123"
    groups = _generate(tmp_path, monkeypatch, fake_global_config, "user:feature")
    assert _shadow(groups)
    waits = [
        s["key"]
        for s in _command_steps(groups)
        if buildkite_step.SELECTOR_SHADOW_KEY in (s.get("depends_on") or [])
    ]
    assert waits == []


def test_none_on_main(tmp_path, monkeypatch, fake_global_config):
    monkeypatch.setenv(recorder_switches.SELECTOR_SHADOW_ENV_VAR, "1")
    assert not _shadow(_generate(tmp_path, monkeypatch, fake_global_config, "main"))


def test_default_branch_is_main(monkeypatch):
    monkeypatch.delenv("VLLM_CI_BRANCH", raising=False)
    (shadow,) = buildkite_step.selector_shadow_group().steps
    assert "/ci-infra/main/buildkite/ci_selector/shadow/run.sh" in shadow.commands[0]
