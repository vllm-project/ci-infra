import os
from contextlib import ExitStack
from unittest.mock import mock_open, patch

import pytest
import yaml

import amd
import buildkite_step
import global_config
import pipeline_generator
from pipeline_generator import (
    check_aiter_nightly_dependencies,
    select_steps_and_dependencies,
)
from step import Step, group_steps

pytestmark = pytest.mark.usefixtures("fake_global_config")

OVERLAY_COMMAND = "bash .buildkite/scripts/rocm/aiter-nightly-overlay.sh"


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
        _mirrored("mi250", "mi250_1"),  # gfx90a: not an AITER arch
        # Built on the overlay's ci_base too (ci-base-image.txt in the artifact).
        _mirrored("dind", "mi355_1", dind=True),
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
    "device, expected",
    [
        ("amd_cpu", True),  # the build lane
        ("mi300_1", True),
        ("mi325_8", True),  # gfx942, like MI300
        ("mi355_4", True),
        ("mi355_dpx", True),
        ("mi250_1", False),
        ("zen5", False),  # an AMD CPU, but not the build lane
        ("h100", False),
        (None, False),
    ],
)
def test_runs_in_aiter_nightly(device, expected):
    assert amd.runs_in_aiter_nightly(device) is expected


def test_overlay_runs_between_ci_base_and_test_image():
    steps = {step.key: step for step in amd.apply_aiter_nightly(_build_steps())}
    assert set(steps) == {
        "refresh-rocm-base-amd",
        "ensure-ci-base-amd",
        "aiter-nightly-amd",
        "image-build-amd",
    }
    overlay, build = steps["aiter-nightly-amd"], steps["image-build-amd"]
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
    assert steps == _build_steps()


@pytest.mark.parametrize("missing", ["ensure-ci-base-amd", "image-build-amd"])
def test_overlay_requires_the_amd_build_steps(missing):
    steps = [step for step in _build_steps() if step.key != missing]
    with pytest.raises(ValueError, match=missing):
        amd.apply_aiter_nightly(steps)


def test_overlay_job_settings(fake_global_config):
    overlay = _render(fake_global_config, "1")["aiter-nightly-amd"]
    statuses = [
        condition.get("exit_status") for condition in overlay.retry["automatic"]
    ]
    assert {1, 2} <= set(statuses)
    # 10 (no wheel) and 11 (the wheel does not install) are AITER failures.
    assert not {10, 11} & set(statuses)
    assert overlay.timeout_in_minutes == amd.AITER_NIGHTLY_TIMEOUT_MINUTES
    assert overlay.agents == {"queue": "amd-cpu"}
    assert not overlay.plugins


def test_nightly_runs_only_aiter_gpu_jobs_unblocked(fake_global_config):
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
        "amd-dind",
        "direct-mi355",
    }
    assert not [key for key, job in jobs.items() if job.deferred]
    # The overlay replaces this build's ci_base-build tag, so native jobs use
    # the usual tag; they just wait for the image build.
    assert jobs["amd-mi300"].env["VLLM_CI_BASE_IMAGE"] == amd.AMD_NATIVE_BASE_IMAGE
    assert jobs["amd-mi300"].depends_on == ["image-build-amd"]


def test_flag_off_renders_the_stock_pipeline(fake_global_config):
    jobs = _render(fake_global_config, "0")
    assert "aiter-nightly-amd" not in jobs
    assert jobs["image-build-amd"].depends_on == ["ensure-ci-base-amd"]
    assert {"cuda-only", "amd-mi250", "amd-dind", "direct-mi250"} <= set(jobs)
    # Steps are deferred as usual when nothing they depend on changed.
    assert jobs["amd-optional"].deferred
    assert jobs["direct-mi355"].deferred


def test_nightly_is_not_gated_by_noauto(fake_global_config, monkeypatch):
    monkeypatch.setenv("NOAUTO", "1")
    jobs = _render(fake_global_config, "1")
    assert not [key for key in jobs if key.startswith("block-")]


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


@pytest.mark.parametrize("key", ["cuda-only", "amd-mi250", "direct-mi250"])
def test_retry_build_rejects_jobs_the_nightly_drops(key):
    steps = amd.apply_aiter_nightly(_steps())
    with pytest.raises(ValueError, match=key):
        select_steps_and_dependencies(steps, frozenset({key}), aiter_nightly=True)


CONFIG_YAML = (
    "name: test\njob_dirs: [/tmp]\nregistries: reg\nrepositories: {main: repo}"
)


def _init_global_config(env):
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


def _converted(fake_global_config, steps):
    fake_global_config["aiter_nightly"] = "1"
    steps = amd.apply_aiter_nightly(steps)
    return buildkite_step.convert_group_step_to_buildkite_step(group_steps(steps))


def test_nightly_rejects_a_kept_job_depending_on_a_dropped_one(fake_global_config):
    steps = _steps() + [_mirrored("needs-cuda", "mi300_1", depends_on=["cuda-only"])]
    with pytest.raises(ValueError, match="amd-needs-cuda -> cuda-only"):
        check_aiter_nightly_dependencies(_converted(fake_global_config, steps))


def _generator(fake_global_config, monkeypatch, tmp_path, steps):
    """A PipelineGenerator for the AITER nightly over steps, and its annotations."""
    fake_global_config.update(aiter_nightly="1", job_dirs=["job_dir"])
    monkeypatch.setattr(
        pipeline_generator, "get_global_config", lambda: fake_global_config
    )
    monkeypatch.setattr(pipeline_generator, "read_steps_from_job_dir", lambda _: steps)
    annotations = []
    monkeypatch.setattr(
        pipeline_generator,
        "_annotate_best_effort",
        lambda text, **kwargs: annotations.append((text, kwargs)),
    )
    generator = object.__new__(pipeline_generator.PipelineGenerator)
    generator.output_file_path = str(tmp_path / "pipeline.yaml")
    return generator, annotations


def test_generate_uploads_the_nightly(fake_global_config, monkeypatch, tmp_path):
    generator, annotations = _generator(
        fake_global_config, monkeypatch, tmp_path, _steps()
    )
    generator.generate()

    uploaded = yaml.safe_load((tmp_path / "pipeline.yaml").read_text())
    jobs = {job["key"]: job for group in uploaded["steps"] for job in group["steps"]}
    assert "cuda-only" not in jobs and "amd-mi250" not in jobs
    assert jobs["aiter-nightly-amd"]["depends_on"] == ["ensure-ci-base-amd"]
    assert jobs["image-build-amd"]["depends_on"] == ["aiter-nightly-amd"]
    # $$ survives to the upload, so Buildkite leaves the $(...) to the job.
    assert any(
        '"$$(buildkite-agent meta-data get aiter-nightly-ci-base)"' in command
        for command in jobs["image-build-amd"]["commands"]
    )
    # Every depends_on names a step in the uploaded pipeline.
    for job in jobs.values():
        for dependency in job.get("depends_on") or []:
            assert dependency in jobs, (job["key"], dependency)
    assert any("AITER nightly" in text for text, _ in annotations)


def test_generate_fails_on_a_dropped_dependency(
    fake_global_config, monkeypatch, tmp_path
):
    steps = _steps() + [_mirrored("needs-cuda", "mi300_1", depends_on=["cuda-only"])]
    generator, annotations = _generator(
        fake_global_config, monkeypatch, tmp_path, steps
    )
    with pytest.raises(ValueError, match="needs-cuda"):
        generator.generate()
    assert any(kwargs == {"style": "error"} for _, kwargs in annotations)
    assert not (tmp_path / "pipeline.yaml").exists()
