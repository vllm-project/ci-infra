"""The Python function recorder opt-in (VLLM_CI_FNREC).

Same shape as the kernel recorder's: off, nothing changes; on, every GPU step
sources the setup script from the generating ci-infra branch and uploads
`.fnrec/**`, while docker-build and no-plugin steps stay untouched. Both
recorders on means one artifact path, not two.
"""

import buildkite_step
import pytest
from step import Step


def _gpu_step(**overrides):
    fields = {
        "label": ":nvidia: (H100) Kernels",
        "key": "kernels",
        "group": "kernels",
        "commands": ["pytest -v -s kernels/core"],
        "device": "h100",
        "num_devices": 1,
        "working_dir": "tests",
    }
    fields.update(overrides)
    return Step(**fields)


def _render(step):
    group = buildkite_step.convert_group_step_to_buildkite_step({step.group: [step]})[0]
    steps = group["steps"] if isinstance(group, dict) else group.steps
    return next(
        s
        for s in steps
        if getattr(s, "key", None) == step.key
        or (isinstance(s, dict) and s.get("key") == step.key)
    )


def _commands(rendered):
    return rendered["commands"] if isinstance(rendered, dict) else rendered.commands


def _artifact_paths(rendered):
    return (
        rendered.get("artifact_paths")
        if isinstance(rendered, dict)
        else rendered.artifact_paths
    )


pytestmark = pytest.mark.usefixtures("fake_global_config")


def test_off_by_default(monkeypatch):
    monkeypatch.delenv(buildkite_step.FNREC_ENV_VAR, raising=False)
    monkeypatch.delenv(buildkite_step.KERNREC_ENV_VAR, raising=False)
    rendered = _render(_gpu_step())
    assert not any("fnrec" in c for c in _commands(rendered))
    assert not _artifact_paths(rendered)


def test_on_arms_gpu_steps_from_the_generating_branch(monkeypatch):
    monkeypatch.setenv(buildkite_step.FNREC_ENV_VAR, "1")
    monkeypatch.delenv(buildkite_step.KERNREC_ENV_VAR, raising=False)
    monkeypatch.setenv("VLLM_CI_BRANCH", "my-branch")
    rendered = _render(_gpu_step())
    commands = _commands(rendered)
    setup = [c for c in commands if "fnrec/ci_setup.sh" in c]
    assert len(setup) == 1
    assert "ci-infra/my-branch/buildkite/ci_selector/fnrec/ci_setup.sh" in setup[0]
    assert commands.index(setup[0]) < commands.index(
        next(c for c in commands if "pytest -v -s kernels/core" in c)
    )
    assert setup[0].rstrip().endswith('echo "fnrec: setup skipped"')
    assert "'" not in setup[0]
    assert _artifact_paths(rendered) == [buildkite_step.KERNREC_ARTIFACT_PATH]


def test_both_recorders_share_one_artifact_path(monkeypatch):
    monkeypatch.setenv(buildkite_step.FNREC_ENV_VAR, "1")
    monkeypatch.setenv(buildkite_step.KERNREC_ENV_VAR, "1")
    rendered = _render(_gpu_step())
    commands = _commands(rendered)
    assert sum("kernrec/ci_setup.sh" in c for c in commands) == 1
    assert sum("fnrec/ci_setup.sh" in c for c in commands) == 1
    assert _artifact_paths(rendered) == [buildkite_step.KERNREC_ARTIFACT_PATH]


@pytest.mark.parametrize(
    "overrides", [{"label": ":docker: build image"}, {"no_plugin": True}]
)
def test_on_leaves_docker_and_no_plugin_steps_alone(monkeypatch, overrides):
    monkeypatch.setenv(buildkite_step.FNREC_ENV_VAR, "1")
    step = _gpu_step(**overrides)
    assert not _fnrec_or_setup(step)


def _fnrec_or_setup(step):
    return buildkite_step._fnrec_applies(step)
