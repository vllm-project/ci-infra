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

    setup = [c for c in commands if "ci_setup.sh" in c]
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


def _rendered_groups(*steps):
    groups = {}
    for s in steps:
        groups.setdefault(s.group, []).append(s)
    return buildkite_step.convert_group_step_to_buildkite_step(groups)


def test_collect_group_depends_on_every_runnable_command_step(
    monkeypatch, fake_global_config
):
    monkeypatch.setenv(buildkite_step.KERNREC_ENV_VAR, "1")
    monkeypatch.setenv("VLLM_CI_BRANCH", "my-branch")
    fake_global_config["nightly"] = "1"  # every step runs, none is blocked
    groups = _rendered_groups(
        _gpu_step(key="kernels", group="kernels"),
        _gpu_step(key="fusion", group="compile", label=":nvidia: (H100) Fusion"),
    )
    collect = buildkite_step.kernrec_collect_group(groups)
    assert collect.group == buildkite_step.KERNREC_COLLECT_GROUP
    (step,) = collect.steps
    assert step.key == buildkite_step.KERNREC_COLLECT_KEY
    assert set(step.depends_on) == {"kernels", "fusion"}
    assert step.allow_dependency_failure is True, (
        "a failed job's recording is still evidence"
    )
    assert step.soft_fail is True, "publishing must not turn the build red"
    assert (
        "ci-infra/my-branch/buildkite/ci_selector/kernrec/collect.sh"
        in step.commands[0]
    )
    assert step.agents["queue"] == buildkite_step.AgentQueue.SMALL_CPU_PREMERGE.value


def test_collect_group_skips_steps_behind_a_block(monkeypatch, fake_global_config):
    monkeypatch.setenv(buildkite_step.KERNREC_ENV_VAR, "1")
    # Not a nightly and no matching file diff: the generator gates the step
    # behind a block step, so the collect step must not depend on it.
    groups = _rendered_groups(_gpu_step(key="kernels", group="kernels"))
    assert any(
        isinstance(s, buildkite_step.BuildkiteBlockStep) for s in groups[0].steps
    )
    (step,) = buildkite_step.kernrec_collect_group(groups).steps
    assert step.depends_on == [], (
        "a dependency that never starts would hold the step forever"
    )


def test_collect_group_uses_postmerge_queue_on_main(monkeypatch, fake_global_config):
    monkeypatch.setenv(buildkite_step.KERNREC_ENV_VAR, "1")
    fake_global_config["branch"] = "main"
    fake_global_config["nightly"] = "1"
    (step,) = buildkite_step.kernrec_collect_group(_rendered_groups(_gpu_step())).steps
    assert step.agents["queue"] == buildkite_step.AgentQueue.SMALL_CPU_POSTMERGE.value


def test_collect_step_serializes_allow_dependency_failure(monkeypatch):
    monkeypatch.setenv(buildkite_step.KERNREC_ENV_VAR, "1")
    (step,) = buildkite_step.kernrec_collect_group(_rendered_groups(_gpu_step())).steps
    assert step.to_yaml()["allow_dependency_failure"] is True
    assert step.dict(exclude_none=True)["allow_dependency_failure"] is True


def test_on_finishes_the_sidecar_as_the_last_command(monkeypatch):
    """The EXIT trap in ci_setup.sh is replaced by vLLM's OTel prelude, so the
    generator records the exit status itself: after the step's commands,
    right before the exit."""
    monkeypatch.setenv(buildkite_step.KERNREC_ENV_VAR, "1")
    monkeypatch.setenv("CONTINUE_ON_FAILURE", "1")
    commands = _commands(_render(_gpu_step()))
    finish = [c for c in commands if "kernrec_finish" in c]
    assert len(finish) == 1
    assert "'" not in finish[0]
    assert "CI_OVERALL_STATUS" in finish[0], "records the status the step exits with"
    test_cmd = next(c for c in commands if "pytest -v -s kernels/core" in c)
    exit_cmd = next(
        c for c in commands if c.startswith("exit ") and "CI_OVERALL_STATUS" in c
    )
    assert (
        commands.index(test_cmd)
        < commands.index(finish[0])
        == commands.index(exit_cmd) - 1
    )


def test_on_finishes_the_sidecar_without_continue_on_failure(monkeypatch):
    monkeypatch.setenv(buildkite_step.KERNREC_ENV_VAR, "1")
    monkeypatch.delenv("CONTINUE_ON_FAILURE", raising=False)
    commands = _commands(_render(_gpu_step()))
    assert "kernrec_finish" in commands[-1], (
        "last, since there is no exit line to precede"
    )


def test_finish_stays_off_with_the_recorder(monkeypatch):
    monkeypatch.delenv(buildkite_step.KERNREC_ENV_VAR, raising=False)
    monkeypatch.setenv("CONTINUE_ON_FAILURE", "1")
    assert not any("kernrec_finish" in c for c in _commands(_render(_gpu_step())))
