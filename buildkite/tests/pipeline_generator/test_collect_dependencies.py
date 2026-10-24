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
    """Optional steps are deferred (excluded from collection) until nightly."""
    monkeypatch.setenv(recorder_switches.KERNREC_ENV_VAR, "1")
    monkeypatch.setenv(recorder_switches.KERNREC_AMD_ENV_VAR, "1")
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
    # In the deferred model, optional steps that cannot run are marked deferred
    # rather than having a block step inserted into their depends_on.
    if not nightly:
        assert rendered.deferred

    collect = _collect(groups, recorder)
    # Deferred steps are excluded from the steps the collect step waits for.
    expected = {regular.key, optional.key} if nightly else {regular.key}
    assert set(collect.env["CI_SELECTOR_COLLECT_WAIT_FOR"].split()) == expected
    assert collect.allow_dependency_failure is True
    if recorder == "fnrec":
        assert collect.env["FNREC_EXPECTED_JOBS"] == ("4" if nightly else "3")


@pytest.mark.parametrize("recorder", ["kernrec", "fnrec"])
@pytest.mark.parametrize("reverse", [False, True])
def test_collection_skips_deferred_steps(recorder, reverse):
    """Deferred steps (those that cannot run without approval) are excluded."""
    setup = {
        "kernrec": buildkite_step.KERNREC_SETUP_PATH,
        "fnrec": buildkite_step.FNREC_SETUP_PATH,
    }[recorder]

    def command(key, dependencies=(), parallelism=None, armed=True, deferred=False):
        return buildkite_step.BuildkiteCommandStep(
            key=key,
            label=key,
            commands=[f"source {setup}"] if armed else ["build image"],
            depends_on=list(dependencies),
            parallelism=parallelism,
            deferred=deferred,
        )

    groups = [
        buildkite_step.BuildkiteGroupStep(
            group="Workers",
            steps=[
                command(
                    "deferred-downstream",
                    ["deferred-worker"],
                    parallelism=2,
                    deferred=True,
                ),
                command("deferred-worker", parallelism=4, deferred=True),
                command("unrelated", ["automatic-image"], parallelism=3),
            ],
        ),
        buildkite_step.BuildkiteGroupStep(
            group="Images",
            steps=[
                command("automatic-image", armed=False),
            ],
        ),
    ]
    if reverse:
        groups.reverse()
    collect = _collect(groups, recorder)
    # It waits for the recording step and depends on that step's prerequisite.
    assert collect.env["CI_SELECTOR_COLLECT_WAIT_FOR"] == "unrelated"
    assert collect.depends_on == ["automatic-image"]
    assert collect.allow_dependency_failure is True
    assert collect.soft_fail is True
    if recorder == "fnrec":
        assert collect.env["FNREC_EXPECTED_JOBS"] == "3"
