import os
import subprocess
from contextlib import ExitStack
from unittest.mock import mock_open, patch

import pytest
import yaml

import amd
import buildkite_step
import global_config
import pipeline_generator
from pipeline_generator import select_steps_and_dependencies
from step import Step, group_steps

pytestmark = pytest.mark.usefixtures("fake_global_config")

BUILD_IMAGE = "rocm/vllm-dev:build-$BUILDKITE_BUILD_ID"


@pytest.fixture
def build_sh_layout(tmp_path, monkeypatch):
    """A scheduled AITER nightly of vllm-project/vllm main, on a checkout that
    builds its ROCm images with build.sh."""
    (tmp_path / ".buildkite/scripts/rocm").mkdir(parents=True)
    (tmp_path / ".buildkite/scripts/rocm/build.sh").touch()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BUILDKITE_REPO", "https://github.com/vllm-project/vllm.git")
    monkeypatch.setenv("BUILDKITE_PULL_REQUEST", "false")
    monkeypatch.setenv("BUILDKITE_BRANCH", "main")
    monkeypatch.setenv("AITER_NIGHTLY", "1")
    monkeypatch.delenv("BUILDKITE_JOB_ID", raising=False)
    monkeypatch.delenv("NOAUTO", raising=False)
    return monkeypatch


def _build_steps():
    """vLLM's .buildkite/hardware_tests/amd.yaml."""
    return [
        Step(
            label=":docker: Ensure ROCm stack + runtime",
            group="Hardware - AMD Build",
            key="refresh-rocm-base-amd",
            depends_on=[],
            device="amd_cpu",
            no_plugin=True,
            commands=["bash .buildkite/scripts/rocm/build.sh rocm base"],
            timeout_in_minutes=540,
            concurrency=1,
            concurrency_group="vllm/rocm/base/rocm/$ROCM_CI_REPO/"
            "$BUILDKITE_PULL_REQUEST/$BUILDKITE_BRANCH",
        ),
        Step(
            label=":docker: Build ROCm test image",
            group="Hardware - AMD Build",
            key="image-build-amd",
            depends_on=["refresh-rocm-base-amd"],
            device="amd_cpu",
            no_plugin=True,
            commands=["bash .buildkite/scripts/rocm/build.sh rocm ci"],
            timeout_in_minutes=180,
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
        mirror={"amd": {"device": device, **mirror}},
    )


def _steps():
    return _build_steps() + [
        Step(label="CUDA image", group="Build", key="image-build", commands=["b"]),
        Step(
            label="CUDA only",
            group="Models",
            key="cuda-only",
            device="h100",
            depends_on=["image-build"],
            commands=["pytest -v -s cuda"],
        ),
        _mirrored("mi300", "mi300_1"),
        _mirrored("mi325", "mi325_2"),
        _mirrored("mi355", "mi355_dpx"),
        _mirrored("mi250", "mi250_1"),  # gfx90a: not an AITER arch
        _mirrored("optional", "mi300_1", optional=True),
        Step(
            label="Direct MI355",
            group="Kernels",
            key="direct-mi355",
            device="mi355_4",
            commands=["pytest -v -s kernels"],
            source_file_dependencies=["csrc/rocm"],
        ),
        Step(
            label="Direct MI250",
            group="Kernels",
            key="direct-mi250",
            device="mi250_1",
            commands=["pytest -v -s kernels"],
        ),
    ]


def _render(fake_global_config, aiter_nightly):
    fake_global_config["aiter_nightly"] = aiter_nightly
    groups = buildkite_step.convert_group_step_to_buildkite_step(group_steps(_steps()))
    return {job.key: job for group in groups for job in group.steps}


def _pod_image(job):
    return job.plugins[0]["kubernetes"]["podSpecPatch"]["containers"][0]["image"]


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


def test_nightly_runs_only_aiter_gpu_jobs_unblocked(
    fake_global_config, build_sh_layout
):
    jobs = _render(fake_global_config, "1")
    assert set(jobs) == {
        "refresh-rocm-base-amd",
        "image-build-amd",
        "amd-mi300",
        "amd-mi325",
        "amd-mi355",
        "amd-optional",
        "direct-mi355",
    }
    assert not [key for key, job in jobs.items() if job.deferred]


def test_nightly_builds_with_vllms_build_steps_into_vllm_dev(
    fake_global_config, build_sh_layout
):
    jobs = _render(fake_global_config, "1")
    base, build = jobs["refresh-rocm-base-amd"], jobs["image-build-amd"]
    # build.sh installs the wheel itself: the build steps are vLLM's, unchanged.
    assert build.depends_on == ["refresh-rocm-base-amd"]
    assert build.commands[-1] == "bash .buildkite/scripts/rocm/build.sh rocm ci"
    # Never the trusted repo, and never its base images' concurrency group.
    assert base.env["ROCM_CI_REPO"] == build.env["ROCM_CI_REPO"] == "rocm/vllm-dev"
    assert base.concurrency_group == (
        "vllm/rocm/base/rocm/rocm/vllm-dev/$BUILDKITE_PULL_REQUEST/$BUILDKITE_BRANCH"
    )
    # Every GPU job is a pod on this build's test image.
    for key in ("amd-mi300", "amd-mi355", "direct-mi355"):
        assert _pod_image(jobs[key]) == BUILD_IMAGE
        assert jobs[key].depends_on[0] == "image-build-amd"


def test_flag_off_renders_the_stock_pipeline(fake_global_config, build_sh_layout):
    build_sh_layout.delenv("AITER_NIGHTLY")
    jobs = _render(fake_global_config, "0")
    assert set(jobs) == {
        "refresh-rocm-base-amd",
        "image-build-amd",
        "image-build",
        "cuda-only",
        "mi300",
        "amd-mi300",
        "mi325",
        "amd-mi325",
        "mi355",
        "amd-mi355",
        "mi250",
        "amd-mi250",
        "optional",
        "amd-optional",
        "direct-mi355",
        "direct-mi250",
    }
    assert jobs["image-build-amd"].env["ROCM_CI_REPO"] == "rocm/vllm-ci"
    # Steps are deferred as usual when nothing they depend on changed.
    assert jobs["amd-optional"].deferred
    assert jobs["direct-mi355"].deferred


def test_nightly_is_not_gated_by_noauto(fake_global_config, build_sh_layout):
    build_sh_layout.setenv("NOAUTO", "1")
    jobs = _render(fake_global_config, "1")
    assert not [key for key, job in jobs.items() if job.deferred]


def test_retry_build_pulls_in_the_build_steps():
    _, selected = select_steps_and_dependencies(
        _steps(), frozenset({"amd-mi355"}), aiter_nightly=True
    )
    assert selected == {"amd-mi355", "image-build-amd", "refresh-rocm-base-amd"}


@pytest.mark.parametrize("key", ["cuda-only", "amd-mi250", "direct-mi250"])
def test_retry_build_rejects_jobs_the_nightly_drops(key):
    with pytest.raises(ValueError, match=key):
        select_steps_and_dependencies(_steps(), frozenset({key}), aiter_nightly=True)


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


@pytest.mark.parametrize(
    "env, aiter_nightly, docs_only_disable",
    [
        ({}, "0", "0"),
        ({"DOCS_ONLY_DISABLE": "1"}, "0", "1"),
        # The nightly never skips docs-only changes.
        ({"AITER_NIGHTLY": "1", "DOCS_ONLY_DISABLE": "0"}, "1", "1"),
    ],
)
def test_nightly_config(env, aiter_nightly, docs_only_disable):
    config = _init_global_config(env)
    assert config["aiter_nightly"] == aiter_nightly
    assert config["docs_only_disable"] == docs_only_disable


def _generator(fake_global_config, monkeypatch, tmp_path, steps):
    """A PipelineGenerator for the AITER nightly over steps, its annotations,
    and the pipelines it uploads early."""
    fake_global_config.update(aiter_nightly="1", job_dirs=["job_dir"])
    monkeypatch.setattr(
        pipeline_generator, "get_global_config", lambda: fake_global_config
    )
    monkeypatch.setattr(pipeline_generator, "read_steps_from_job_dir", lambda _: steps)
    annotations, uploads = [], []
    monkeypatch.setattr(
        pipeline_generator,
        "_annotate_best_effort",
        lambda text, **kwargs: annotations.append((text, kwargs)),
    )

    def fake_run(cmd, *args, **kwargs):
        assert cmd[:3] == ["buildkite-agent", "pipeline", "upload"], cmd
        with open(cmd[3]) as f:
            uploads.append(yaml.safe_load(f))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(pipeline_generator.subprocess, "run", fake_run)
    generator = object.__new__(pipeline_generator.PipelineGenerator)
    generator.output_file_path = str(tmp_path / "out" / "pipeline.yaml")
    os.makedirs(tmp_path / "out")
    return generator, annotations, uploads


def _jobs(pipeline):
    return {job["key"]: job for group in pipeline["steps"] for job in group["steps"]}


def test_generate_uploads_the_nightly(
    fake_global_config, build_sh_layout, monkeypatch, tmp_path
):
    generator, _, uploads = _generator(
        fake_global_config, monkeypatch, tmp_path, _steps()
    )
    generator.generate()

    jobs = _jobs(yaml.safe_load((tmp_path / "out/pipeline.yaml").read_text()))
    assert set(jobs) == {
        "refresh-rocm-base-amd",
        "image-build-amd",
        "amd-mi300",
        "amd-mi325",
        "amd-mi355",
        "amd-optional",
        "direct-mi355",
    }
    assert uploads == []
    # Every depends_on names a step in the uploaded pipeline.
    for job in jobs.values():
        for dependency in job.get("depends_on") or []:
            assert dependency in jobs, (job["key"], dependency)


def test_generate_uploads_only_the_amd_image_steps_early(
    fake_global_config, build_sh_layout, monkeypatch, tmp_path
):
    build_sh_layout.setenv("BUILDKITE_JOB_ID", "job-1")
    generator, _, uploads = _generator(
        fake_global_config, monkeypatch, tmp_path, _steps()
    )
    generator.generate()

    (early,) = uploads
    assert set(_jobs(early)) == {"refresh-rocm-base-amd", "image-build-amd"}
    final = _jobs(yaml.safe_load((tmp_path / "out/pipeline.yaml").read_text()))
    assert not {"refresh-rocm-base-amd", "image-build-amd", "image-build"} & set(final)
    assert "amd-mi300" in final


def _depends_on_a_dropped_step(fake_global_config):
    return _steps() + [_mirrored("needs-cuda", "mi300_1", depends_on=["cuda-only"])]


def _with_torch_nightly(fake_global_config):
    fake_global_config["torch_nightly"] = "1"
    return _steps()


def _without_build_sh(fake_global_config):
    # Older vLLM checkouts would silently test the stock AITER.
    os.remove(".buildkite/scripts/rocm/build.sh")
    return _steps()


@pytest.mark.parametrize(
    "setup, error",
    [
        (_depends_on_a_dropped_step, "amd-needs-cuda -> cuda-only"),
        (_with_torch_nightly, "TORCH_NIGHTLY and AITER_NIGHTLY"),
        (_without_build_sh, "build.sh"),
    ],
)
def test_generate_fails_before_uploading_anything(
    fake_global_config, build_sh_layout, monkeypatch, tmp_path, setup, error
):
    # A real build: the image steps would otherwise go up early, and run with
    # no tests after them.
    build_sh_layout.setenv("BUILDKITE_JOB_ID", "job-1")
    steps = setup(fake_global_config)
    generator, annotations, uploads = _generator(
        fake_global_config, monkeypatch, tmp_path, steps
    )
    with pytest.raises(ValueError, match=error):
        generator.generate()
    assert [kwargs for text, kwargs in annotations if error in text] == [
        {"style": "error"}
    ]
    assert uploads == []
    assert not (tmp_path / "out/pipeline.yaml").exists()


def test_dependency_check_sees_the_jobs_the_nightly_uploads(
    fake_global_config, build_sh_layout
):
    """aiter_nightly_jobs predicts, before the early upload, what
    convert_group_step_to_buildkite_step emits."""
    rendered = _render(fake_global_config, "1")
    assert pipeline_generator.aiter_nightly_jobs(_steps()) == {
        key: job.depends_on or [] for key, job in rendered.items()
    }
