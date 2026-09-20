"""The kernel-launch recorder opt-in (VLLM_CI_KERNREC).

Off, nothing changes: no setup command, no artifact paths. On, every GPU step
sources the setup script from the generating ci-infra branch and uploads
`.fnrec/**`, while AMD, docker-build and no-plugin steps stay untouched.
"""

import buildkite_step
import pytest
from step import Step

pytestmark = pytest.mark.usefixtures("fake_global_config")


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


def test_off_by_default(monkeypatch):
    monkeypatch.delenv(buildkite_step.KERNREC_ENV_VAR, raising=False)
    rendered = _render(_gpu_step())
    assert not any("kernrec" in c for c in _commands(rendered))
    assert not _artifact_paths(rendered)


def test_on_arms_gpu_steps_from_the_generating_branch(monkeypatch):
    monkeypatch.setenv(buildkite_step.KERNREC_ENV_VAR, "1")
    monkeypatch.setenv("VLLM_CI_BRANCH", "my-branch")
    rendered = _render(_gpu_step())
    commands = _commands(rendered)

    setup = [c for c in commands if "kernrec" in c]
    assert len(setup) == 1
    assert "ci-infra/my-branch/buildkite/ci_selector/kernrec/ci_setup.sh" in setup[0]
    # Setup runs before the step's own commands and after the cd, so the
    # script itself must find the checkout root (it uses git for that).
    assert commands.index(setup[0]) < commands.index(
        next(c for c in commands if "pytest -v -s kernels/core" in c)
    )
    # A download failure must not fail the step.
    assert setup[0].rstrip().endswith('echo "kernrec: setup skipped"')
    # _prepare_commands turns single quotes into double quotes; the command
    # must not depend on any.
    assert "'" not in setup[0]
    assert _artifact_paths(rendered) == [buildkite_step.KERNREC_ARTIFACT_PATH]


def test_on_defaults_to_main_branch(monkeypatch):
    monkeypatch.setenv(buildkite_step.KERNREC_ENV_VAR, "1")
    monkeypatch.delenv("VLLM_CI_BRANCH", raising=False)
    rendered = _render(_gpu_step())
    setup = next(c for c in _commands(rendered) if "kernrec" in c)
    assert "ci-infra/main/buildkite/ci_selector/kernrec/ci_setup.sh" in setup


def test_on_leaves_docker_build_steps_alone(monkeypatch):
    monkeypatch.setenv(buildkite_step.KERNREC_ENV_VAR, "1")
    rendered = _render(
        _gpu_step(
            label=":docker: build image",
            key="image-build",
            device=None,
            working_dir=None,
        )
    )
    assert not any("kernrec" in c for c in _commands(rendered))
    assert not _artifact_paths(rendered)


def test_on_leaves_no_plugin_steps_alone(monkeypatch):
    monkeypatch.setenv(buildkite_step.KERNREC_ENV_VAR, "1")
    rendered = _render(_gpu_step(no_plugin=True))
    assert not any("kernrec" in c for c in _commands(rendered))
    assert not _artifact_paths(rendered)
