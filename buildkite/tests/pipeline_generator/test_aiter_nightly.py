import json
import subprocess

import pytest
import yaml

import amd
import buildkite_step
import pipeline_generator
import selector_gate
from constants import Platform
from pipeline_generator import select_steps_and_dependencies
from step import Step


def _step(key, device, **fields):
    return Step(
        label=key,
        group="Tests",
        key=key,
        device=device,
        commands=["pytest -v -s tests"],
        **fields,
    )


def _mirrored(key, device, **mirror):
    return _step(key, "h100", mirror={"amd": {"device": device, **mirror}})


def _steps():
    return [
        # vLLM's .buildkite/hardware_tests/amd.yaml
        Step(
            label="Ensure ROCm stack + runtime",
            group="AMD Build",
            key="refresh-rocm-base-amd",
            device="amd_cpu",
            no_plugin=True,
            commands=["bash .buildkite/scripts/rocm/build.sh rocm base"],
            concurrency=1,
            concurrency_group="vllm/rocm/base/$ROCM_CI_REPO",
        ),
        Step(
            label="Build ROCm test image",
            group="AMD Build",
            key="image-build-amd",
            depends_on=["refresh-rocm-base-amd"],
            device="amd_cpu",
            no_plugin=True,
            commands=["bash .buildkite/scripts/rocm/build.sh rocm ci"],
        ),
        Step(label="CUDA image", group="Build", key="image-build", commands=["b"]),
        _step("cuda-only", "h100", depends_on=["image-build"]),
        _mirrored("mi300", "mi300_1"),
        _mirrored("mi325", "mi325_2"),  # gfx942, like MI300
        _mirrored("mi355", "mi355_dpx"),
        _mirrored("mi250", "mi250_1"),  # gfx90a: AITER builds no kernels for it
        _mirrored("optional", "mi300_1", optional=True),
        _mirrored("dind-mirror", "mi300_1", dind=True),
        _step("no-device-mirror", "h100", mirror={"amd": {}}),
        _step("a100", "a100", mirror={"amd": {"device": "mi355_1"}}),
        _step("direct-mi355", "mi355_4", dind=False, automatic_shard=True),
        _step("direct-mi250", "mi250_1", dind=False, automatic_shard=True),
        # An AMD GPU job emits no mirror of itself.
        _step("native-with-mirror", "mi300_1", mirror={"amd": {"device": "mi355_1"}}),
        # e.g. from a group default; the uploaded job waits for image-build-amd.
        _step("native-on-image-build", "mi355_1", depends_on=["image-build"]),
        Step(label="Unkeyed MI300", group="Tests", device="mi300_1", commands=["t"]),
        _step("amd-cpu-tool", "amd_cpu", depends_on=["image-build-amd"]),
    ]


NIGHTLY_JOBS = {
    "refresh-rocm-base-amd",
    "image-build-amd",
    "amd-mi300",
    "amd-mi325",
    "amd-mi355",
    "amd-optional",
    "amd-dind-mirror",
    "amd-a100",
    "direct-mi355",
    "native-with-mirror",
    "native-on-image-build",
    buildkite_step._generate_step_key("Unkeyed MI300"),
    "amd-cpu-tool",
}


def _jobs(pipeline):
    return {job["key"]: job for group in pipeline["steps"] for job in group["steps"]}


class AiterNightlyBuild:
    """A scheduled AITER nightly of vllm-project/vllm main, on a checkout with
    build.sh. Change `config`, `steps` or the environment, then `generate()`.
    Commands are recorded in `calls`, not run."""

    def __init__(self, config, checkout):
        self.config = config
        self.steps = _steps()
        self.checkout = checkout
        self.pipeline_file = checkout / "out" / "pipeline.yaml"
        self.calls = []
        self.early_jobs = {}  # uploaded ahead of the pipeline
        self.sharded_keys = []

    def generate(self):
        """Returns the pipeline's jobs, by key."""
        generator = object.__new__(pipeline_generator.PipelineGenerator)
        generator.output_file_path = str(self.pipeline_file)
        generator.generate()
        return _jobs(yaml.safe_load(self.pipeline_file.read_text()))

    @property
    def uploads(self):
        return [cmd for cmd in self.calls if "upload" in cmd]

    @property
    def error_annotations(self):
        return [
            cmd[2]
            for cmd in self.calls
            if cmd[1] == "annotate" and cmd[-2:] == ["--style", "error"]
        ]

    @property
    def selector_record(self):
        path = self.pipeline_file.parent / selector_gate.DECISION_FILE
        return json.loads(path.read_text())

    def _run(self, cmd, *args, **kwargs):
        self.calls.append(cmd)
        if cmd[1:3] == ["pipeline", "upload"]:
            with open(cmd[3]) as f:
                self.early_jobs.update(_jobs(yaml.safe_load(f)))
        return subprocess.CompletedProcess(cmd, 0)

    def _plan_step(self, key, *args):
        self.sharded_keys.append(key)
        return {"shards": 3, "commands": [], "overBudget": False}, "plan"


@pytest.fixture
def build(fake_global_config, monkeypatch, tmp_path):
    build_sh = tmp_path / ".buildkite/scripts/rocm/build.sh"
    build_sh.parent.mkdir(parents=True)
    build_sh.touch()
    (tmp_path / "out").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BUILDKITE_REPO", "https://github.com/vllm-project/vllm.git")
    monkeypatch.setenv("BUILDKITE_PULL_REQUEST", "false")
    monkeypatch.setenv("BUILDKITE_BRANCH", "main")
    monkeypatch.setenv("BUILDKITE_JOB_ID", "job-1")  # enables the early upload
    monkeypatch.setenv("AITER_NIGHTLY", "1")
    monkeypatch.delenv("NOAUTO", raising=False)
    monkeypatch.delenv("VLLM_CI_RUNTIME_SHARD", raising=False)
    fake_global_config.update(job_dirs=["jobs"], aiter_nightly="1")

    build = AiterNightlyBuild(fake_global_config, tmp_path)
    monkeypatch.setattr(pipeline_generator, "get_global_config", lambda: build.config)
    monkeypatch.setattr(
        pipeline_generator, "read_steps_from_job_dir", lambda _: build.steps
    )
    monkeypatch.setattr(subprocess, "run", build._run)
    # Would fetch test timings from Buildkite.
    monkeypatch.setattr(buildkite_step.runtime_shard, "plan_step", build._plan_step)
    return build


@pytest.mark.parametrize(
    "device, expected",
    [
        ("amd_cpu", True),  # the build lane
        ("mi300_1", True),
        ("mi325_8", True),
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


@pytest.mark.parametrize("platform", [None, Platform.AMD])
def test_runs_the_amd_build_steps_and_gfx942_gfx950_jobs(build, platform):
    build.config["platform"] = platform

    jobs = build.generate()

    assert set(jobs) == NIGHTLY_JOBS
    for job in jobs.values():
        assert set(job.get("depends_on") or []) <= set(jobs), job["key"]
    assert jobs["native-on-image-build"]["depends_on"] == ["image-build-amd"]
    assert build.early_jobs == {}  # everything goes up in one upload


def test_builds_into_vllm_dev_with_vllms_build_sh(build):
    jobs = build.generate()

    base, image = jobs["refresh-rocm-base-amd"], jobs["image-build-amd"]
    assert image["depends_on"] == ["refresh-rocm-base-amd"]
    assert image["commands"][-1] == "bash .buildkite/scripts/rocm/build.sh rocm ci"
    assert base["env"]["ROCM_CI_REPO"] == "rocm/vllm-dev"
    assert image["env"]["ROCM_CI_REPO"] == "rocm/vllm-dev"
    assert base["concurrency_group"] == "vllm/rocm/base/rocm/vllm-dev"
    pod = jobs["amd-mi355"]["plugins"][0]["kubernetes"]["podSpecPatch"]
    assert pod["containers"][0]["image"] == "rocm/vllm-dev:build-$BUILDKITE_BUILD_ID"


def test_ignores_noauto_docs_only_and_the_recorders(build, monkeypatch):
    monkeypatch.setenv("NOAUTO", "1")  # nobody would unblock a scheduled build
    build.config.update(list_file_diff=["docs/index.md"], docs_only_disable="0")
    # Its records would mislead the test selector.
    for recorder in ("VLLM_CI_KERNREC", "VLLM_CI_KERNREC_AMD", "VLLM_CI_FNREC"):
        monkeypatch.setenv(recorder, "1")

    jobs = build.generate()

    assert set(jobs) == NIGHTLY_JOBS  # no block steps, not skipped
    pipeline = build.pipeline_file.read_text()
    for marker in ("kernrec", "fnrec", "VLLM_KERNEL_SYMBOL_MAP"):
        assert marker not in pipeline


def test_shards_and_records_only_its_own_jobs(build, monkeypatch):
    monkeypatch.setenv(selector_gate.SELECTOR_ENV_VAR, "on")

    jobs = build.generate()

    assert build.sharded_keys == ["direct-mi355"]
    assert jobs["direct-mi355"]["parallelism"] == 3
    record = build.selector_record
    assert record["skipped"].startswith("AITER nightly")
    assert set(record["legacy_keys"]) == set(record["scheduled_keys"]) == set(jobs)


@pytest.mark.parametrize("value", ["0", "true"])
def test_flag_off_runs_the_stock_pipeline(build, monkeypatch, value):
    monkeypatch.setenv("AITER_NIGHTLY", value)
    # The regular nightly: every step runs, none deferred.
    build.config.update(aiter_nightly=value, nightly="1")

    jobs = build.generate()

    early = build.early_jobs
    assert {"image-build", "image-build-amd", "refresh-rocm-base-amd"} == set(early)
    assert {"cuda-only", "amd-mi250", "direct-mi250"} <= set(jobs)
    assert early["image-build-amd"]["env"]["ROCM_CI_REPO"] == "rocm/vllm-ci"


def test_rerun_runs_the_named_job_and_its_build_steps(build, monkeypatch):
    monkeypatch.setenv("NOAUTO", "1")
    build.config["only_step_keys"] = frozenset({"native-on-image-build"})

    jobs = build.generate()

    assert set(jobs) == {
        "native-on-image-build",
        "image-build-amd",
        "refresh-rocm-base-amd",
    }


def _depends_on_cuda(build):
    build.steps.append(_mirrored("needs-cuda", "mi300_1", depends_on=["cuda-only"]))


def _torch_nightly(build):
    build.config.update(torch_nightly="1", platform=Platform.CUDA)


def _cuda_platform(build):
    build.config["platform"] = Platform.CUDA


def _no_build_sh(build):
    # Older checkouts would silently test the stock AITER.
    (build.checkout / ".buildkite/scripts/rocm/build.sh").unlink()


def _rerun(*keys):
    return lambda build: build.config.update(only_step_keys=frozenset(keys))


@pytest.mark.parametrize(
    "setup, error",
    [
        (_depends_on_cuda, "cuda-only"),
        (_torch_nightly, "AITER_NIGHTLY runs only AMD jobs"),
        (_cuda_platform, "AITER_NIGHTLY runs only AMD jobs"),
        (_no_build_sh, "build.sh"),
        (_rerun("cuda-only"), "cuda-only"),
        (_rerun("amd-mi250"), "amd-mi250"),
        (_rerun("amd-native-with-mirror"), "amd-native-with-mirror"),
    ],
)
def test_fails_before_uploading_anything(build, setup, error):
    setup(build)

    with pytest.raises(ValueError, match=error):
        build.generate()

    (annotation,) = build.error_annotations
    assert error in annotation
    assert build.uploads == []
    assert not build.pipeline_file.exists()


def test_rerun_of_an_amd_job_pulls_in_the_amd_image_build():
    # Any rerun, not only the AITER nightly's: the job is uploaded waiting for
    # image-build-amd.
    _, selected = select_steps_and_dependencies(
        _steps(), frozenset({"native-on-image-build"})
    )
    assert selected == {
        "native-on-image-build",
        "image-build-amd",
        "refresh-rocm-base-amd",
    }
