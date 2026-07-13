import subprocess
import sys
from pathlib import Path

import pytest

import amd
import buildkite_step
import step as step_module
from constants import AgentQueue
from step import Step, group_steps, read_steps_from_job_dir

TEST_JOB_DIR = Path(__file__).resolve().parent / "test_files" / "test_jobs"


@pytest.fixture(autouse=True)
def fake_global_config(monkeypatch):
    monkeypatch.delenv(buildkite_step.SKIP_TIMEOUT_ENV_VAR, raising=False)
    config = {
        "name": "vllm_ci",
        "github_repo_name": "vllm-project/vllm",
        "job_dirs": [],
        "registries": "example.com/vllm",
        "repositories": {
            "main": "vllm-ci-postmerge-repo",
            "premerge": "vllm-ci-test-repo",
        },
        "branch": "test-branch",
        "commit": "abc123",
        "pull_request": "false",
        "docs_only_disable": "1",
        "nightly": "0",
        "torch_nightly": "0",
        "run_all": False,
        "list_file_diff": [],
        "fail_fast": False,
    }
    monkeypatch.setattr(step_module, "get_global_config", lambda: config)
    monkeypatch.setattr(buildkite_step, "get_global_config", lambda: config)
    monkeypatch.setattr(
        buildkite_step,
        "get_ecr_cache_registry",
        lambda: ("cache-from", "cache-to"),
    )
    monkeypatch.setattr(
        buildkite_step,
        "get_image",
        lambda cpu=False, arm64=False: "test-image",
    )
    monkeypatch.setattr(
        buildkite_step,
        "get_torch_nightly_image",
        lambda: "torch-nightly-image",
    )
    return config


def _render_single_step(step):
    return buildkite_step.convert_group_step_to_buildkite_step({
        step.group: [step],
    })[0]


def test_read_steps_from_job_dir():
    steps = read_steps_from_job_dir(str(TEST_JOB_DIR))
    steps_by_label = {step.label: step for step in steps}

    assert len(steps) == 8
    assert steps_by_label["Test A"].group == "bb"
    assert steps_by_label["Test A"].commands == [
        'echo "Test A"',
        'echo "Test A.B"',
    ]
    assert steps_by_label["Test D"].num_nodes == 2
    assert steps_by_label["Test D"].num_devices == 4
    assert steps_by_label["Test E"].group == "a"


def test_group_steps_sorts_steps_within_each_group():
    steps = read_steps_from_job_dir(str(TEST_JOB_DIR))
    grouped_steps = group_steps(steps)

    assert set(grouped_steps) == {"a", "bb"}
    assert [step.label for step in grouped_steps["a"]] == [
        "Test E",
        "Test F",
        "Test G",
        "Test H",
    ]
    assert [step.label for step in grouped_steps["bb"]] == [
        "Test A",
        "Test B",
        "Test C",
        "Test D",
    ]


@pytest.mark.parametrize(
    ("device", "queue", "native_ci", "expected_gpu_count"),
    [
        ("mi300_4", AgentQueue.AMD_MI300_4, True, "4"),
        ("mi300_4", AgentQueue.AMD_MI300_4, False, "4"),
        ("mi325_1", AgentQueue.AMD_MI325_1, True, "1"),
        ("mi325_1", AgentQueue.AMD_MI325_1, False, "1"),
    ],
)
def test_direct_amd_gpu_steps_use_tagged_runtime_policy(
    device, queue, native_ci, expected_gpu_count
):
    step = Step(
        label="AMD direct test",
        group="Direct AMD",
        key=f"amd-direct-{device}",
        depends_on=["image-build"],
        device=device,
        native_ci=native_ci,
        optional=True,
        soft_fail=True,
        working_dir="/vllm-workspace/tests",
        commands=["pytest tests/foo.py"],
    )

    group_step = _render_single_step(step)
    block_step, command_step = group_step.steps

    assert group_step.group == "Hardware-AMD Tests"
    assert block_step.block == f"Run AMD: AMD direct test ({device})"
    assert block_step.depends_on == ["image-build-amd"]
    assert command_step.label == f"AMD: AMD direct test ({device})"
    assert command_step.depends_on == ["image-build-amd", block_step.key]
    assert command_step.agents == {"queue": queue}
    assert command_step.commands == [
        "bash .buildkite/scripts/hardware_ci/run-amd-test.sh",
    ]
    if native_ci:
        assert command_step.plugins is not None
        pod_patch = command_step.plugins[0]["kubernetes"]["podSpecPatch"]
        container = pod_patch["containers"][0]
        assert container["resources"]["limits"]["amd.com/gpu"] == expected_gpu_count
        assert container["resources"]["requests"]["amd.com/gpu"] == expected_gpu_count
        assert command_step.env["AMD_CI_RUNTIME"] == "native"
        assert command_step.env["VLLM_CI_EXPECTED_GPU_COUNT"] == expected_gpu_count
        assert "DOCKER_IMAGE_NAME" not in command_step.env
    else:
        assert command_step.plugins is None
        assert "AMD_CI_RUNTIME" not in command_step.env
        assert command_step.env["DOCKER_IMAGE_NAME"] == amd.AMD_STABLE_CI_BASE_IMAGE

    assert command_step.retry == amd.AMD_RETRY
    assert len(command_step.retry["automatic"]) == 5

    test_commands = command_step.env["VLLM_TEST_COMMANDS"]
    assert test_commands.startswith(f"export VLLM_TEST_GROUP_NAME={step.key}")
    assert "(command amd-smi || true)" in test_commands
    assert "ROCm debug agent disabled" in test_commands
    assert amd.ROCM_DEBUG_AGENT_ENV_VAR in test_commands
    assert "if test -f /opt/rocm/lib/librocm-debug-agent.so.2" not in test_commands
    assert "[ -f /opt/rocm/lib/librocm-debug-agent.so.2" not in test_commands
    assert "export HSA_TOOLS_LIB=" not in test_commands
    assert "HSA_ENABLE_DEBUG=1" not in test_commands
    assert "WARNING: ROCm debug agent not found at" not in test_commands
    assert "cd /vllm-workspace/tests" in test_commands
    assert "pytest tests/foo.py" in test_commands
    assert "nvidia-smi" not in test_commands
    assert "CUDA_ENABLE_COREDUMP_ON_EXCEPTION" not in test_commands


def test_amd_device_rejects_conflicting_gpu_count():
    step = Step(
        label="AMD GPU count mismatch",
        group="Direct AMD",
        device="mi300_4",
        num_devices=2,
        commands=["pytest tests/example.py"],
    )

    with pytest.raises(
        ValueError,
        match=r"AMD device mi300_4 provides 4 GPUs, but num_devices=2",
    ):
        _render_single_step(step)


def test_rocm_debug_agent_setup_is_opt_in(monkeypatch):
    monkeypatch.setenv(amd.ROCM_DEBUG_AGENT_ENV_VAR, "1")
    step = Step(
        label="AMD debug test",
        group="Direct AMD",
        key="amd-debug",
        depends_on=["image-build"],
        device="mi300_4",
        optional=True,
        working_dir="/vllm-workspace/tests",
        commands=["pytest tests/debug.py"],
    )

    group_step = _render_single_step(step)
    _, command_step = group_step.steps

    test_commands = command_step.env["VLLM_TEST_COMMANDS"]
    assert "if test -f /opt/rocm/lib/librocm-debug-agent.so.2" in test_commands
    assert (
        "export HSA_TOOLS_LIB=/opt/rocm/lib/librocm-debug-agent.so.2"
        in test_commands
    )
    assert "HSA_ENABLE_DEBUG=1" in test_commands
    assert "ROCm debug agent enabled" in test_commands
    assert "WARNING: ROCm debug agent not found at" in test_commands


def test_continue_on_failure_exits_nonzero_after_command_failure(monkeypatch):
    monkeypatch.setenv("CONTINUE_ON_FAILURE", "1")
    step = Step(
        label="Continue on failure",
        group="Failure handling",
        commands=[
            "echo before",
            "false",
            "echo after",
        ],
    )

    commands = buildkite_step._prepare_commands(
        step,
        variables_to_inject={},
        setup_profile="none",
    )
    script = " && ".join(commands).replace("$$CI_OVERALL_STATUS", "$CI_OVERALL_STATUS")

    result = subprocess.run(
        ["bash", "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert "__CI_OVERALL_STATUS" not in script
    assert "CI_OVERALL_STATUS=1" in script
    assert "after" in result.stdout
    assert result.returncode == 1


def test_multi_gpu_step_dumps_nvidia_topology():
    step = Step(
        label="Distributed Comm Ops Test",
        group="Distributed",
        key="distributed-comm-ops",
        depends_on=["image-build"],
        device="h100",
        num_devices=2,
        working_dir="/vllm-workspace/tests",
        commands=["pytest tests/distributed/test_comm_ops.py"],
    )

    commands = buildkite_step._prepare_commands(step, variables_to_inject={})

    assert '(command nvidia-smi topo -m || true)' in commands
    # Topology dump comes after the base GPU info and before coredump setup.
    topo_index = commands.index('(command nvidia-smi topo -m || true)')
    smi_index = commands.index('(command nvidia-smi || true)')
    assert smi_index < topo_index


def test_multi_node_step_dumps_nvidia_topology():
    step = Step(
        label="Multi-node Test",
        group="Distributed",
        key="multi-node",
        depends_on=["image-build"],
        device="h100",
        num_nodes=2,
        num_devices=4,
        working_dir="/vllm-workspace/tests",
        commands=["pytest tests/distributed/test_multi_node.py"],
    )

    commands = buildkite_step._prepare_commands(step, variables_to_inject={})

    assert '(command nvidia-smi topo -m || true)' in commands


def test_single_gpu_step_skips_nvidia_topology():
    step = Step(
        label="Single GPU Test",
        group="Single",
        key="single-gpu",
        depends_on=["image-build"],
        device="h100",
        num_devices=1,
        working_dir="/vllm-workspace/tests",
        commands=["pytest tests/basic.py"],
    )

    commands = buildkite_step._prepare_commands(step, variables_to_inject={})

    # Base GPU info is still emitted, but the topology dump is multi-GPU only.
    assert '(command nvidia-smi || true)' in commands
    assert '(command nvidia-smi topo -m || true)' not in commands


def test_amd_mirror_uses_shared_gating_with_amd_dependency_fallback(
    fake_global_config,
):
    fake_global_config["list_file_diff"] = ["vllm/model_executor/foo.py"]
    step = Step(
        label="Mirrored test",
        group="Mirrors",
        key="mirrored-test",
        depends_on=["image-build"],
        working_dir="/vllm-workspace/tests",
        commands=["pytest tests/mirror.py"],
        source_file_dependencies=["vllm/"],
        mirror={
            "amd": {
                "device": "mi325_1",
                "depends_on": ["image-build-amd"],
                "soft_fail": False,
                "source_file_dependencies": ["amd-only/"],
            }
        },
    )

    group_steps = buildkite_step.convert_group_step_to_buildkite_step({
        step.group: [step],
    })
    default_group = next(group for group in group_steps if group.group == "Mirrors")
    default_command_step = next(
        s for s in default_group.steps
        if isinstance(s, buildkite_step.BuildkiteCommandStep)
    )
    amd_group = next(
        group for group in group_steps if group.group == "Hardware-AMD Tests"
    )
    amd_command_step = next(
        s for s in amd_group.steps
        if isinstance(s, buildkite_step.BuildkiteCommandStep)
    )

    assert default_command_step.depends_on == ["image-build"]
    assert default_command_step.soft_fail is False
    assert len(amd_group.steps) == 1
    assert amd_command_step.depends_on == ["image-build-amd"]
    assert amd_command_step.agents == {"queue": AgentQueue.AMD_MI325_1}
    assert amd_command_step.soft_fail is True
    assert "ROCm debug agent disabled" in (
        amd_command_step.env["VLLM_TEST_COMMANDS"]
    )


def test_native_tagged_mirror_uses_native_runner_gating(
    fake_global_config,
):
    fake_global_config["list_file_diff"] = [
        ".buildkite/scripts/hardware_ci/run-amd-test.sh"
    ]
    step = Step(
        label="Native mirrored test",
        group="Mirrors",
        commands=["pytest tests/mirror.py"],
        source_file_dependencies=["vllm/"],
        device="h200_18gb",
        mirror={"amd": {"device": "mi325_1", "native_ci": True}},
    )

    group_steps = buildkite_step.convert_group_step_to_buildkite_step(
        {
            step.group: [step],
        }
    )
    default_group = next(group for group in group_steps if group.group == "Mirrors")
    amd_group = next(
        group for group in group_steps if group.group == "Hardware-AMD Tests"
    )

    assert isinstance(default_group.steps[0], buildkite_step.BuildkiteBlockStep)
    assert len(amd_group.steps) == 1
    amd_command_step = amd_group.steps[0]
    assert isinstance(amd_command_step, buildkite_step.BuildkiteCommandStep)
    assert amd_command_step.plugins is not None


def test_untagged_mi300_mirror_does_not_use_native_runner_gating(
    fake_global_config,
):
    fake_global_config["list_file_diff"] = [
        ".buildkite/scripts/hardware_ci/run-amd-test.sh"
    ]
    step = Step(
        label="DinD mirrored test",
        group="Mirrors",
        commands=["pytest tests/mirror.py"],
        source_file_dependencies=["vllm/"],
        device="h200_18gb",
        mirror={"amd": {"device": "mi300_1"}},
    )

    group_steps = buildkite_step.convert_group_step_to_buildkite_step(
        {
            step.group: [step],
        }
    )
    amd_group = next(
        group for group in group_steps if group.group == "Hardware-AMD Tests"
    )

    assert isinstance(amd_group.steps[0], buildkite_step.BuildkiteBlockStep)
    amd_command_step = amd_group.steps[1]
    assert isinstance(amd_command_step, buildkite_step.BuildkiteCommandStep)
    assert amd_command_step.plugins is None
    assert amd_command_step.env["DOCKER_IMAGE_NAME"] == amd.AMD_STABLE_CI_BASE_IMAGE


def test_torch_nightly_flag_no_separate_group(fake_global_config):
    # TORCH_NIGHTLY=1 now runs the entire existing pipeline against the nightly
    # base image (built by image_build.sh when TORCH_NIGHTLY=1, CUDA/GPU lane).
    # It must NOT synthesize a separate "vLLM Against PyTorch Nightly" group.
    fake_global_config["torch_nightly"] = "1"
    step = Step(
        label="Untagged test",
        group="Some Group",
        key="untagged-test",
        depends_on=["image-build"],
        working_dir="/vllm-workspace/tests",
        commands=["pytest tests/untagged.py"],
        source_file_dependencies=["tests/untagged.py"],
        device="h200_18gb",
    )

    group_steps = buildkite_step.convert_group_step_to_buildkite_step({
        step.group: [step],
    })

    # No dedicated torch-nightly group is synthesized anymore.
    assert not any(
        g.group == "vLLM Against PyTorch Nightly" for g in group_steps
    )

    # The step stays in its normal group and is built once (no nightly duplicate).
    normal_group = next(g for g in group_steps if g.group == "Some Group")
    labels = [
        s.label for s in normal_group.steps
        if isinstance(s, buildkite_step.BuildkiteCommandStep)
    ]
    assert "Untagged test" in labels
    assert not any(lbl.startswith("Torch Nightly ") for lbl in labels)


def test_timeout_in_minutes_propagates_to_command_step():
    step = Step(
        label="Timed test",
        group="Timing",
        key="timed-test",
        depends_on=["image-build"],
        working_dir="/vllm-workspace/tests",
        commands=["pytest tests/timed.py"],
        device="h200_18gb",
        timeout_in_minutes=42,
    )

    group_step = _render_single_step(step)
    command_step = next(
        s for s in group_step.steps
        if isinstance(s, buildkite_step.BuildkiteCommandStep)
    )

    assert command_step.timeout_in_minutes == 42


def test_skip_timeout_omits_timeout_from_command_step(monkeypatch):
    monkeypatch.setenv(buildkite_step.SKIP_TIMEOUT_ENV_VAR, "1")
    step = Step(
        label="Skipped timeout",
        group="Timing",
        commands=["pytest tests/timed.py"],
        device="h200_18gb",
        timeout_in_minutes=42,
    )

    group_step = _render_single_step(step)
    command_step = next(
        s for s in group_step.steps
        if isinstance(s, buildkite_step.BuildkiteCommandStep)
    )

    assert command_step.timeout_in_minutes is None
    assert "timeout_in_minutes" not in command_step.model_dump(exclude_none=True)


def test_missing_timeout_in_minutes_is_omitted_from_pipeline():
    step = Step(
        label="Untimed test",
        group="Timing",
        key="untimed-test",
        depends_on=["image-build"],
        working_dir="/vllm-workspace/tests",
        commands=["pytest tests/untimed.py"],
        device="h200_18gb",
    )

    group_step = _render_single_step(step)
    command_step = next(
        s for s in group_step.steps
        if isinstance(s, buildkite_step.BuildkiteCommandStep)
    )

    assert command_step.timeout_in_minutes is None
    # exclude_none is used when dumping the pipeline, so an unset timeout must
    # not surface as a key at all.
    assert "timeout_in_minutes" not in command_step.model_dump(exclude_none=True)


def test_direct_amd_gpu_step_propagates_timeout():
    step = Step(
        label="AMD direct timed",
        group="Direct AMD",
        key="amd-direct-timed",
        depends_on=["image-build"],
        device="mi300_4",
        working_dir="/vllm-workspace/tests",
        commands=["pytest tests/foo.py"],
        timeout_in_minutes=90,
    )

    group_step = _render_single_step(step)
    command_step = next(
        s for s in group_step.steps
        if isinstance(s, buildkite_step.BuildkiteCommandStep)
    )

    assert command_step.timeout_in_minutes == 90


def test_skip_timeout_omits_direct_amd_timeout(monkeypatch):
    monkeypatch.setenv(buildkite_step.SKIP_TIMEOUT_ENV_VAR, "1")
    step = Step(
        label="AMD direct skipped timeout",
        group="Direct AMD",
        device="mi300_4",
        commands=["pytest tests/foo.py"],
        timeout_in_minutes=90,
    )

    group_step = _render_single_step(step)
    command_step = next(
        s for s in group_step.steps
        if isinstance(s, buildkite_step.BuildkiteCommandStep)
    )

    assert command_step.timeout_in_minutes is None
    assert "timeout_in_minutes" not in command_step.model_dump(exclude_none=True)


def test_amd_mirror_uses_its_own_timeout_in_minutes(fake_global_config):
    fake_global_config["list_file_diff"] = ["vllm/foo.py"]
    step = Step(
        label="Mirrored timed test",
        group="Mirrors",
        key="mirrored-timed",
        depends_on=["image-build"],
        working_dir="/vllm-workspace/tests",
        commands=["pytest tests/mirror.py"],
        source_file_dependencies=["vllm/"],
        device="h200_18gb",
        timeout_in_minutes=40,
        mirror={
            "amd": {
                "device": "mi325_1",
                "depends_on": ["image-build-amd"],
                "timeout_in_minutes": 75,
            }
        },
    )

    group_steps = buildkite_step.convert_group_step_to_buildkite_step({
        step.group: [step],
    })
    default_group = next(group for group in group_steps if group.group == "Mirrors")
    default_command_step = next(
        s for s in default_group.steps
        if isinstance(s, buildkite_step.BuildkiteCommandStep)
    )
    amd_group = next(
        group for group in group_steps if group.group == "Hardware-AMD Tests"
    )
    amd_command_step = next(
        s for s in amd_group.steps
        if isinstance(s, buildkite_step.BuildkiteCommandStep)
    )

    # The main step keeps its own timeout; the AMD mirror uses the (larger)
    # timeout declared on the mirror block.
    assert default_command_step.timeout_in_minutes == 40
    assert amd_command_step.timeout_in_minutes == 75


def test_skip_timeout_omits_main_and_amd_mirror_timeouts(
    monkeypatch, fake_global_config,
):
    monkeypatch.setenv(buildkite_step.SKIP_TIMEOUT_ENV_VAR, "1")
    fake_global_config["list_file_diff"] = ["vllm/foo.py"]
    step = Step(
        label="Mirrored skipped timeout",
        group="Mirrors",
        commands=["pytest tests/mirror.py"],
        source_file_dependencies=["vllm/"],
        device="h200_18gb",
        timeout_in_minutes=40,
        mirror={
            "amd": {
                "device": "mi325_1",
                "depends_on": ["image-build-amd"],
                "timeout_in_minutes": 75,
            }
        },
    )

    group_steps = buildkite_step.convert_group_step_to_buildkite_step({
        step.group: [step],
    })
    command_steps = [
        command_step
        for group_step in group_steps
        for command_step in group_step.steps
        if isinstance(command_step, buildkite_step.BuildkiteCommandStep)
    ]

    assert len(command_steps) == 2
    assert all(step.timeout_in_minutes is None for step in command_steps)
    assert all(
        "timeout_in_minutes" not in step.model_dump(exclude_none=True)
        for step in command_steps
    )


def test_amd_mirror_without_timeout_stays_unbounded(fake_global_config):
    fake_global_config["list_file_diff"] = ["vllm/foo.py"]
    step = Step(
        label="Mirrored untimed test",
        group="Mirrors",
        key="mirrored-untimed",
        depends_on=["image-build"],
        working_dir="/vllm-workspace/tests",
        commands=["pytest tests/mirror.py"],
        source_file_dependencies=["vllm/"],
        device="h200_18gb",
        timeout_in_minutes=40,
        mirror={
            "amd": {
                "device": "mi325_1",
                "depends_on": ["image-build-amd"],
            }
        },
    )

    group_steps = buildkite_step.convert_group_step_to_buildkite_step({
        step.group: [step],
    })
    amd_group = next(
        group for group in group_steps if group.group == "Hardware-AMD Tests"
    )
    amd_command_step = next(
        s for s in amd_group.steps
        if isinstance(s, buildkite_step.BuildkiteCommandStep)
    )

    # An AMD mirror without its own timeout must not inherit the shorter main
    # timeout (AMD runs slower); it stays unbounded until one is declared.
    assert amd_command_step.timeout_in_minutes is None


if __name__ == "__main__":
    sys.exit(pytest.main(["-v", __file__]))
