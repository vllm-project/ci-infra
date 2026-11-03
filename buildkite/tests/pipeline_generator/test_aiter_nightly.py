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


@pytest.fixture
def run(fake_global_config, monkeypatch, tmp_path):
    """Generate a scheduled build of vllm-project/vllm main, on a checkout with
    build.sh. Returns the final and early-uploaded jobs, and records the agent
    calls, annotations, runtime-shard plans and selector record."""
    (tmp_path / ".buildkite/scripts/rocm").mkdir(parents=True)
    (tmp_path / ".buildkite/scripts/rocm/build.sh").touch()
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
    monkeypatch.setattr(
        pipeline_generator, "get_global_config", lambda: fake_global_config
    )
    steps = _steps()
    monkeypatch.setattr(pipeline_generator, "read_steps_from_job_dir", lambda _: steps)

    calls, annotations, planned, records, early = [], [], [], [], {}

    def fake_run(cmd, *args, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["buildkite-agent", "pipeline", "upload"]:
            with open(cmd[3]) as f:
                early.update(_jobs(yaml.safe_load(f)))
        return subprocess.CompletedProcess(cmd, 0)

    def plan_step(key, *args):
        planned.append(key)
        return {"shards": 3, "commands": [], "overBudget": False}, "plan"

    monkeypatch.setattr(pipeline_generator.subprocess, "run", fake_run)
    monkeypatch.setattr(
        pipeline_generator,
        "_annotate_best_effort",
        lambda text, **kwargs: annotations.append((text, kwargs)),
    )
    monkeypatch.setattr(buildkite_step.runtime_shard, "plan_step", plan_step)
    monkeypatch.setattr(buildkite_step.runtime_shard, "annotate", lambda *a: None)
    monkeypatch.setattr(
        selector_gate, "write_and_upload", lambda r, _: records.append(r)
    )
    monkeypatch.setattr(selector_gate, "annotate", lambda record: None)

    out = tmp_path / "out"
    out.mkdir()

    def generate():
        generator = object.__new__(pipeline_generator.PipelineGenerator)
        generator.output_file_path = str(out / "pipeline.yaml")
        generator.generate()
        return _jobs(yaml.safe_load((out / "pipeline.yaml").read_text())), early

    generate.config, generate.steps, generate.out = fake_global_config, steps, out
    generate.calls, generate.annotations = calls, annotations
    generate.planned, generate.records = planned, records
    return generate


def _jobs(pipeline):
    return {job["key"]: job for group in pipeline["steps"] for job in group["steps"]}


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
def test_nightly_runs_only_the_amd_lane(run, monkeypatch, noauto):
    if noauto:
        monkeypatch.setenv("NOAUTO", "1")
    monkeypatch.setenv(selector_gate.SELECTOR_ENV_VAR, "on")
    # A docs-only last commit does not skip it.
    run.config.update(list_file_diff=["docs/index.md"], docs_only_disable="0")

    jobs, early = run()

    # Nothing gated or deferred: no block, no "Run more jobs", no early upload.
    assert set(jobs) == NIGHTLY_JOBS
    assert early == {}
    assert not any("artifact" in cmd for cmd in run.calls)
    for job in jobs.values():
        for dependency in job.get("depends_on") or []:
            assert dependency in jobs, (job["key"], dependency)
    # vLLM's build steps, unchanged, into rocm/vllm-dev.
    base, build = jobs["refresh-rocm-base-amd"], jobs["image-build-amd"]
    assert build["depends_on"] == ["refresh-rocm-base-amd"]
    assert build["commands"][-1] == "bash .buildkite/scripts/rocm/build.sh rocm ci"
    assert (
        base["env"]["ROCM_CI_REPO"] == build["env"]["ROCM_CI_REPO"] == "rocm/vllm-dev"
    )
    assert base["concurrency_group"] == "vllm/rocm/base/rocm/vllm-dev"
    pod = jobs["amd-mi355"]["plugins"][0]["kubernetes"]["podSpecPatch"]
    assert pod["containers"][0]["image"] == "rocm/vllm-dev:build-$BUILDKITE_BUILD_ID"
    assert jobs["native-on-image-build"]["depends_on"] == ["image-build-amd"]
    # Runtime sharding plans only the jobs it keeps.
    assert run.planned == ["direct-mi355"]
    assert jobs["direct-mi355"]["parallelism"] == 3
    # The selector is skipped, and its record names only these jobs.
    (record,) = run.records
    assert record["skipped"].startswith("AITER nightly")
    assert set(record["legacy_keys"]) == set(record["scheduled_keys"]) == set(jobs)


@pytest.mark.parametrize("value", ["0", "true"])
def test_flag_off_runs_the_stock_pipeline(run, monkeypatch, value):
    monkeypatch.setenv("AITER_NIGHTLY", value)
    # The regular nightly: every step runs, none deferred.
    run.config.update(aiter_nightly=value, nightly="1")

    jobs, early = run()

    assert {"image-build", "image-build-amd", "refresh-rocm-base-amd"} == set(early)
    assert {"cuda-only", "amd-mi250", "direct-mi250"} <= set(jobs)
    assert early["image-build-amd"]["env"]["ROCM_CI_REPO"] == "rocm/vllm-ci"


def test_retry_build_runs_the_named_job_and_its_build_steps(run, monkeypatch):
    monkeypatch.setenv("NOAUTO", "1")
    run.config["only_step_keys"] = frozenset({"native-on-image-build"})

    jobs, _ = run()

    assert set(jobs) == {
        "native-on-image-build",
        "image-build-amd",
        "refresh-rocm-base-amd",
    }


def _depends_on_cuda(run, monkeypatch):
    run.steps.append(_mirrored("needs-cuda", "mi300_1", depends_on=["cuda-only"]))


def _torch_nightly(run, monkeypatch):
    run.config["torch_nightly"] = "1"


def _no_build_sh(run, monkeypatch):
    # Older checkouts would silently test the stock AITER.
    (run.out.parent / ".buildkite/scripts/rocm/build.sh").unlink()


def _recorder(name):
    # Its records feed the test selector; this run would skew them.
    return lambda run, monkeypatch: monkeypatch.setenv(name, "1")


def _retry(*keys):
    return lambda run, monkeypatch: run.config.update(only_step_keys=frozenset(keys))


@pytest.mark.parametrize(
    "setup, error",
    [
        (_depends_on_cuda, "cuda-only"),
        (_torch_nightly, "TORCH_NIGHTLY and AITER_NIGHTLY"),
        (_no_build_sh, "build.sh"),
        (_recorder("VLLM_CI_KERNREC"), "cannot record"),
        (_recorder("VLLM_CI_KERNREC_AMD"), "cannot record"),
        (_recorder("VLLM_CI_FNREC"), "cannot record"),
        (_retry("cuda-only"), "cuda-only"),
        (_retry("amd-mi250"), "amd-mi250"),
        (_retry("amd-native-with-mirror"), "amd-native-with-mirror"),
    ],
)
def test_fails_before_uploading_anything(run, monkeypatch, setup, error):
    setup(run, monkeypatch)

    with pytest.raises(ValueError, match=error):
        run()

    assert [kw for text, kw in run.annotations if error in text] == [{"style": "error"}]
    assert run.calls == []
    assert not (run.out / "pipeline.yaml").exists()


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
