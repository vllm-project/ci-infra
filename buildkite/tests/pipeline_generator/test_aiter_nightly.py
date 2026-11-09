import subprocess

import pytest
import yaml

import amd
import buildkite_step
import pipeline_generator
import selector_gate
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
    """A scheduled AITER nightly build of vllm-project/vllm main, on a
    checkout with build.sh. Adjust `config`, `steps` or the environment, then
    call `generate()`. Nothing leaves the process: what the generator would
    send out is recorded on the attributes below."""

    def __init__(self, config, checkout):
        self.config = config  # the global config the generator reads
        self.steps = _steps()  # what every job dir yields
        self.checkout = checkout  # the vLLM checkout, also the cwd
        self.pipeline_file = checkout / "out" / "pipeline.yaml"
        self.agent_calls = []  # every subprocess.run command
        self.early_jobs = {}  # jobs uploaded before the pipeline, by key
        self.annotations = []  # (text, kwargs) of each build annotation
        self.sharded_keys = []  # steps runtime sharding planned
        self.selector_records = []  # the selector's decision records

    def generate(self):
        """Run the generator; returns the uploaded pipeline's jobs, by key."""
        generator = object.__new__(pipeline_generator.PipelineGenerator)
        generator.output_file_path = str(self.pipeline_file)
        generator.generate()
        return _jobs(yaml.safe_load(self.pipeline_file.read_text()))

    def _run(self, cmd, *args, **kwargs):
        self.agent_calls.append(cmd)
        if cmd[:3] == ["buildkite-agent", "pipeline", "upload"]:
            with open(cmd[3]) as f:
                self.early_jobs.update(_jobs(yaml.safe_load(f)))
        return subprocess.CompletedProcess(cmd, 0)

    def _plan_step(self, key, *args):
        self.sharded_keys.append(key)
        return {"shards": 3, "commands": [], "overBudget": False}, "plan"


@pytest.fixture
def build(fake_global_config, monkeypatch, tmp_path):
    (tmp_path / ".buildkite/scripts/rocm").mkdir(parents=True)
    (tmp_path / ".buildkite/scripts/rocm/build.sh").touch()
    (tmp_path / "out").mkdir()
    monkeypatch.chdir(tmp_path)
    for name, value in {
        "BUILDKITE_REPO": "https://github.com/vllm-project/vllm.git",
        "BUILDKITE_PULL_REQUEST": "false",
        "BUILDKITE_BRANCH": "main",
        "BUILDKITE_JOB_ID": "job-1",  # so the early image upload applies
        "AITER_NIGHTLY": "1",
    }.items():
        monkeypatch.setenv(name, value)
    for name in ("NOAUTO", "VLLM_CI_RUNTIME_SHARD"):
        monkeypatch.delenv(name, raising=False)
    fake_global_config.update(job_dirs=["jobs"], aiter_nightly="1")

    build = AiterNightlyBuild(fake_global_config, tmp_path)
    monkeypatch.setattr(pipeline_generator, "get_global_config", lambda: build.config)
    monkeypatch.setattr(
        pipeline_generator, "read_steps_from_job_dir", lambda _: build.steps
    )
    monkeypatch.setattr(pipeline_generator.subprocess, "run", build._run)
    monkeypatch.setattr(
        pipeline_generator,
        "_annotate_best_effort",
        lambda text, **kwargs: build.annotations.append((text, kwargs)),
    )
    monkeypatch.setattr(buildkite_step.runtime_shard, "plan_step", build._plan_step)
    monkeypatch.setattr(buildkite_step.runtime_shard, "annotate", lambda *a: None)
    monkeypatch.setattr(
        selector_gate,
        "write_and_upload",
        lambda record, _: build.selector_records.append(record),
    )
    monkeypatch.setattr(selector_gate, "annotate", lambda record: None)
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


@pytest.mark.parametrize("noauto", [False, True])
def test_nightly_runs_only_the_amd_lane(build, monkeypatch, noauto):
    if noauto:
        monkeypatch.setenv("NOAUTO", "1")
    # Ignored: its records would mislead the test selector.
    for recorder in ("VLLM_CI_KERNREC", "VLLM_CI_KERNREC_AMD", "VLLM_CI_FNREC"):
        monkeypatch.setenv(recorder, "1")
    monkeypatch.setenv(selector_gate.SELECTOR_ENV_VAR, "on")
    # A docs-only last commit does not skip it.
    build.config.update(list_file_diff=["docs/index.md"], docs_only_disable="0")

    jobs = build.generate()

    # Nothing gated or deferred: no block, no "Run more jobs", no early upload,
    # no recording or collect step.
    assert set(jobs) == NIGHTLY_JOBS
    uploaded = build.pipeline_file.read_text()
    for marker in ("kernrec", "fnrec", "VLLM_KERNEL_SYMBOL_MAP"):
        assert marker not in uploaded
    assert build.early_jobs == {}
    assert not any("artifact" in cmd for cmd in build.agent_calls)
    for job in jobs.values():
        for dependency in job.get("depends_on") or []:
            assert dependency in jobs, (job["key"], dependency)
    # vLLM's build steps, unchanged, into rocm/vllm-dev.
    base, image = jobs["refresh-rocm-base-amd"], jobs["image-build-amd"]
    assert image["depends_on"] == ["refresh-rocm-base-amd"]
    assert image["commands"][-1] == "bash .buildkite/scripts/rocm/build.sh rocm ci"
    assert (
        base["env"]["ROCM_CI_REPO"] == image["env"]["ROCM_CI_REPO"] == "rocm/vllm-dev"
    )
    assert base["concurrency_group"] == "vllm/rocm/base/rocm/vllm-dev"
    pod = jobs["amd-mi355"]["plugins"][0]["kubernetes"]["podSpecPatch"]
    assert pod["containers"][0]["image"] == "rocm/vllm-dev:build-$BUILDKITE_BUILD_ID"
    assert jobs["native-on-image-build"]["depends_on"] == ["image-build-amd"]
    # Runtime sharding plans only the jobs it keeps.
    assert build.sharded_keys == ["direct-mi355"]
    assert jobs["direct-mi355"]["parallelism"] == 3
    # The selector is skipped, and its record names only these jobs.
    (record,) = build.selector_records
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


def test_retry_build_runs_the_named_job_and_its_build_steps(build, monkeypatch):
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
    build.config["torch_nightly"] = "1"


def _no_build_sh(build):
    # Older checkouts would silently test the stock AITER.
    (build.checkout / ".buildkite/scripts/rocm/build.sh").unlink()


def _retry(*keys):
    return lambda build: build.config.update(only_step_keys=frozenset(keys))


@pytest.mark.parametrize(
    "setup, error",
    [
        (_depends_on_cuda, "cuda-only"),
        (_torch_nightly, "TORCH_NIGHTLY and AITER_NIGHTLY"),
        (_no_build_sh, "build.sh"),
        (_retry("cuda-only"), "cuda-only"),
        (_retry("amd-mi250"), "amd-mi250"),
        (_retry("amd-native-with-mirror"), "amd-native-with-mirror"),
    ],
)
def test_fails_before_uploading_anything(build, setup, error):
    setup(build)

    with pytest.raises(ValueError, match=error):
        build.generate()

    errors = [kw for text, kw in build.annotations if error in text]
    assert errors == [{"style": "error"}]
    assert build.agent_calls == []
    assert not build.pipeline_file.exists()


def test_retry_of_an_amd_job_pulls_in_the_amd_image_build():
    # Any retry build: the job is uploaded waiting for image-build-amd.
    _, selected = select_steps_and_dependencies(
        _steps(), frozenset({"native-on-image-build"})
    )
    assert selected == {
        "native-on-image-build",
        "image-build-amd",
        "refresh-rocm-base-amd",
    }
