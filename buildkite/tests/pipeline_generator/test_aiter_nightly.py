import os
from contextlib import ExitStack
from unittest.mock import mock_open, patch

import pytest

import amd
import buildkite_step
from pipeline_generator import select_steps_and_dependencies
from step import Step, group_steps

pytestmark = pytest.mark.usefixtures("fake_global_config")

OVERLAY_COMMAND = "bash .buildkite/scripts/rocm/aiter-nightly-overlay.sh"
NATIVE_TAG = "rocm/vllm-dev:ci_base-build-$BUILDKITE_BUILD_ID"


def _build_steps():
    def build_step(label, key, depends_on, command, env=None):
        return Step(
            label=label,
            group="Hardware - AMD Build",
            key=key,
            depends_on=depends_on,
            device="amd_cpu",
            no_plugin=True,
            commands=[command],
            env=env,
        )

    return [
        build_step(
            "AMD: :docker: refresh ROCm base",
            "refresh-rocm-base-amd",
            [],
            "bash .buildkite/scripts/rocm/refresh-base-image.sh",
        ),
        build_step(
            "AMD: :docker: ensure ci_base",
            "ensure-ci-base-amd",
            ["refresh-rocm-base-amd"],
            "bash .buildkite/scripts/rocm/build-ci-base.sh",
        ),
        build_step(
            "AMD: :docker: build test image and artifacts",
            "image-build-amd",
            ["ensure-ci-base-amd"],
            "bash .buildkite/scripts/rocm/build-test-image.sh",
            env={"IMAGE_TAG": "rocm/vllm-ci:build-$BUILDKITE_BUILD_ID"},
        ),
    ]


def _mirrored(key, device, **mirror):
    return Step(
        label=key,
        group="Models",
        key=key,
        device="h100",
        commands=["pytest -v -s models"],
        source_file_dependencies=["vllm/model_executor"],
        mirror={"amd": {"device": device, "dind": False, **mirror}},
    )


def _steps():
    return _build_steps() + [
        Step(
            label="CUDA only",
            group="Models",
            key="cuda-only",
            device="h100",
            commands=["pytest -v -s cuda"],
        ),
        _mirrored("mi300", "mi300_1"),
        _mirrored("mi325", "mi325_2"),
        _mirrored("mi355", "mi355_dpx"),
        _mirrored("mi250", "mi250_1"),  # AITER builds no kernels for gfx90a
        _mirrored("dind", "mi355_1", dind=True),  # tests the stable ci_base
        _mirrored("optional", "mi300_1", optional=True),
        Step(
            label="Direct MI355",
            group="Kernels",
            key="direct-mi355",
            device="mi355_4",
            dind=False,
            commands=["pytest -v -s kernels"],
            source_file_dependencies=["csrc/rocm"],
        ),
        Step(
            label="Direct MI250",
            group="Kernels",
            key="direct-mi250",
            device="mi250_1",
            dind=False,
            commands=["pytest -v -s kernels"],
        ),
    ]


def _render(fake_global_config, aiter_nightly):
    fake_global_config["aiter_nightly"] = aiter_nightly
    steps = _steps()
    if aiter_nightly == "1":
        steps = amd.apply_aiter_nightly(steps)
    groups = buildkite_step.convert_group_step_to_buildkite_step(group_steps(steps))
    return {job.key: job for group in groups for job in group.steps}


@pytest.mark.parametrize(
    "device, dind, expected",
    [
        ("mi300_1", False, True),
        ("mi325_8", False, True),  # gfx942, like MI300
        ("mi355_4", False, True),
        ("mi355_dpx", False, True),
        ("mi250_1", False, False),
        ("mi355_1", True, False),
        ("amd_cpu", False, False),
        ("h100", False, False),
    ],
)
def test_runs_in_aiter_nightly(device, dind, expected):
    assert amd.runs_in_aiter_nightly(device, dind) is expected


def test_overlay_runs_between_ci_base_and_test_image():
    steps = amd.apply_aiter_nightly(_build_steps())
    assert [step.key for step in steps] == [
        "refresh-rocm-base-amd",
        "ensure-ci-base-amd",
        "aiter-nightly-amd",
        "image-build-amd",
    ]
    overlay, build = steps[2], steps[3]
    assert overlay.depends_on == ["ensure-ci-base-amd"]
    assert overlay.commands == [OVERLAY_COMMAND]
    assert overlay.group == "Hardware - AMD Build"

    assert build.depends_on == ["aiter-nightly-amd"]
    # Point the ci_base handoff at the nightly image, then build as usual. $$
    # defers the substitution from pipeline upload to the job.
    assert build.commands == [
        "buildkite-agent meta-data set rocm-ci-base-image "
        '"$$(buildkite-agent meta-data get aiter-nightly-ci-base)"',
        "bash .buildkite/scripts/rocm/build-test-image.sh",
    ]
    assert build.env == {
        "IMAGE_TAG": "rocm/vllm-ci:build-$BUILDKITE_BUILD_ID",
        "ROCM_CACHE_BRANCH_NAME": "aiter-nightly",
        "ROCM_CONTENT_CACHE_EXPORT_MODE": "never",
    }


def test_overlay_does_not_change_the_steps_it_was_given():
    steps = _build_steps()
    amd.apply_aiter_nightly(steps)
    assert steps[2].depends_on == ["ensure-ci-base-amd"]
    assert len(steps[2].commands) == 1


@pytest.mark.parametrize("missing", ["ensure-ci-base-amd", "image-build-amd"])
def test_overlay_requires_the_amd_build_steps(missing):
    steps = [step for step in _build_steps() if step.key != missing]
    with pytest.raises(ValueError, match=missing):
        amd.apply_aiter_nightly(steps)


def test_overlay_step_retries_only_transient_failures(fake_global_config):
    overlay = _render(fake_global_config, "1")["aiter-nightly-amd"]
    statuses = [
        condition.get("exit_status") for condition in overlay.retry["automatic"]
    ]
    assert 1 in statuses and 2 in statuses
    # 10 (no wheel) and 11 (the wheel does not install) are AITER failures.
    assert 10 not in statuses and 11 not in statuses
    assert overlay.timeout_in_minutes == amd.AITER_NIGHTLY_TIMEOUT_MINUTES
    assert overlay.agents == {"queue": "amd-cpu"}
    assert not overlay.plugins


def test_nightly_runs_only_native_aiter_gpu_jobs_unblocked(fake_global_config):
    jobs = _render(fake_global_config, "1")
    assert set(jobs) == {
        "refresh-rocm-base-amd",
        "ensure-ci-base-amd",
        "aiter-nightly-amd",
        "image-build-amd",
        "amd-mi300",
        "amd-mi325",
        "amd-mi355",
        "amd-optional",
        "direct-mi355",
    }
    # The overlay replaces this build's ci_base-build tag, so native jobs use
    # the usual tag; they just wait for the image build.
    assert jobs["amd-mi300"].env["VLLM_CI_BASE_IMAGE"] == NATIVE_TAG
    assert jobs["amd-mi300"].depends_on == ["image-build-amd"]


def test_flag_off_renders_the_stock_pipeline(fake_global_config):
    jobs = _render(fake_global_config, "0")
    assert "aiter-nightly-amd" not in jobs
    assert jobs["image-build-amd"].depends_on == ["ensure-ci-base-amd"]
    assert {"cuda-only", "amd-mi250", "amd-dind", "direct-mi250"} <= set(jobs)
    # Steps are gated as usual when nothing they depend on changed.
    assert "block-amd-optional" in jobs
    assert "block-amd-direct-mi355" in jobs


def test_dind_jobs_on_aiter_gpus_are_reported():
    assert amd.aiter_nightly_dind_steps(_steps()) == ["amd-dind"]


def test_retry_build_pulls_in_the_overlay():
    steps = amd.apply_aiter_nightly(_steps())
    _, selected = select_steps_and_dependencies(
        steps, frozenset({"amd-mi355"}), aiter_nightly=True
    )
    assert selected == {
        "amd-mi355",
        "image-build-amd",
        "aiter-nightly-amd",
        "ensure-ci-base-amd",
        "refresh-rocm-base-amd",
    }


@pytest.mark.parametrize("key", ["cuda-only", "amd-mi250", "amd-dind"])
def test_retry_build_rejects_jobs_the_nightly_drops(key):
    steps = amd.apply_aiter_nightly(_steps())
    with pytest.raises(ValueError, match=key):
        select_steps_and_dependencies(steps, frozenset({key}), aiter_nightly=True)


CONFIG_YAML = (
    "name: test\njob_dirs: [/tmp]\nregistries: reg\nrepositories: {main: repo}"
)


def _init_global_config(env):
    import global_config

    global_config.config = None
    patches = [
        patch("global_config.get_merge_base_commit", return_value="sha"),
        patch("global_config.get_list_file_diff", return_value=[]),
        patch("global_config.get_pr_labels", return_value=[]),
        patch("builtins.open", mock_open(read_data=CONFIG_YAML)),
        patch("os.path.exists", return_value=True),
        patch.dict(os.environ, {"BUILDKITE_BRANCH": "main", **env}, clear=True),
    ]
    with ExitStack() as stack:
        for active in patches:
            stack.enter_context(active)
        try:
            global_config.init_global_config("dummy_path")
            return global_config.config
        finally:
            global_config.config = None


def test_nightly_is_off_by_default():
    config = _init_global_config({})
    assert config["aiter_nightly"] == "0"
    assert config["docs_only_disable"] == "0"


def test_nightly_never_skips_docs_only_changes():
    config = _init_global_config({"AITER_NIGHTLY": "1", "DOCS_ONLY_DISABLE": "0"})
    assert config["aiter_nightly"] == "1"
    assert config["docs_only_disable"] == "1"


def test_nightly_cannot_combine_with_torch_nightly():
    with pytest.raises(ValueError, match="TORCH_NIGHTLY and AITER_NIGHTLY"):
        _init_global_config({"AITER_NIGHTLY": "1", "TORCH_NIGHTLY": "1"})
