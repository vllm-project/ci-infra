"""Collectors wait for recording jobs that can run without manual approval."""

import buildkite_step
import pytest
import recorder_switches
from step import Step

pytestmark = pytest.mark.usefixtures("fake_global_config")


def _collect(groups, recorder):
    collect = getattr(buildkite_step, f"{recorder}_collect_group")
    return collect(groups).steps[0]


@pytest.mark.parametrize("recorder", ["kernrec", "fnrec"])
@pytest.mark.parametrize("nightly", [False, True])
def test_direct_optional_amd_job_does_not_hold_collection(
    monkeypatch, fake_global_config, recorder, nightly
):
    """The real LM-eval shape has a block-amd-amd-* block for an amd-* job."""
    monkeypatch.setenv(recorder_switches.KERNREC_ENV_VAR, "1")
    monkeypatch.setenv(recorder_switches.FNREC_ENV_VAR, "1")
    fake_global_config.update(run_all=True, nightly="1" if nightly else "0")
    regular = Step(
        label=":nvidia: (H100) Kernels",
        key="regular-kernels",
        device="h100",
        commands=["pytest -v -s kernels/core"],
        working_dir="tests",
        parallelism=3,
    )
    optional = Step(
        label=":amd: (MI300) LM Eval Large Models",
        key="amd-lm-eval-large-models",
        device="mi300_8",
        dind=False,
        optional=True,
        commands=["pytest -s -v evals/gsm8k/test_gsm8k_correctness.py"],
        working_dir="tests",
        depends_on=["image-build-amd"],
    )
    groups = buildkite_step.convert_group_step_to_buildkite_step(
        {"Tests": [regular, optional]}
    )
    rendered = next(s for g in groups for s in g.steps if s.key == optional.key)
    assert buildkite_step._carries(rendered, recorder), "the optional job is armed"
    if not nightly:
        assert "block-amd-amd-lm-eval-large-models" in rendered.depends_on

    collect = _collect(groups, recorder)
    expected = {regular.key, optional.key} if nightly else {regular.key}
    assert set(collect.depends_on) == expected
    assert collect.allow_dependency_failure is True
    if recorder == "fnrec":
        assert collect.env["FNREC_EXPECTED_JOBS"] == ("4" if nightly else "3")


@pytest.mark.parametrize("recorder", ["kernrec", "fnrec"])
@pytest.mark.parametrize("reverse", [False, True])
def test_collection_follows_block_dependencies_across_groups(recorder, reverse):
    """An armed worker cannot run if an upstream image/job needs approval."""
    setup = {
        "kernrec": buildkite_step.KERNREC_SETUP_PATH,
        "fnrec": buildkite_step.FNREC_SETUP_PATH,
    }[recorder]

    def command(key, dependencies=(), parallelism=None, armed=True):
        return buildkite_step.BuildkiteCommandStep(
            key=key,
            label=key,
            commands=[f"source {setup}"] if armed else ["build image"],
            depends_on=list(dependencies),
            parallelism=parallelism,
        )

    groups = [
        buildkite_step.BuildkiteGroupStep(
            group="Workers",
            steps=[
                command("downstream", ["blocked-worker"], parallelism=2),
                command("blocked-worker", ["manual-image"], parallelism=4),
                command("unrelated", ["automatic-image"], parallelism=3),
            ],
        ),
        buildkite_step.BuildkiteGroupStep(
            group="Images",
            steps=[
                # The name resembles an unrelated command. Only the actual
                # dependency edge establishes which jobs this block gates.
                buildkite_step.BuildkiteBlockStep(
                    key="block-unrelated", block="Approve optional image"
                ),
                command("manual-image", ["block-unrelated"], armed=False),
                command("automatic-image", armed=False),
            ],
        ),
    ]
    if reverse:
        groups.reverse()
    collect = _collect(groups, recorder)
    assert set(collect.depends_on) == {"unrelated", "automatic-image"}
    assert collect.allow_dependency_failure is True
    assert collect.soft_fail is True
    if recorder == "fnrec":
        assert collect.env["FNREC_EXPECTED_JOBS"] == "3"
