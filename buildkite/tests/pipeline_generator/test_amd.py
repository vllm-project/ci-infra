import pytest

import amd
import buildkite_step
from constants import AgentQueue
from step import Step

pytestmark = pytest.mark.usefixtures("fake_global_config")


def _render_single_step(step):
    return buildkite_step.convert_group_step_to_buildkite_step(
        {
            step.group: [step],
        }
    )[0]


def _retry_exit_statuses(command_step):
    return {
        rule["exit_status"]
        for rule in command_step.retry["automatic"]
        if "exit_status" in rule
    }


@pytest.mark.parametrize("value", [True, False])
def test_hf_offline_retry_is_strictly_typed_for_direct_and_amd_mirror(value):
    direct = Step.from_yaml(
        {
            "label": "Direct AMD",
            "device": "mi300_1",
            "commands": ["pytest tests/example.py"],
            "hf_offline_retry": value,
        }
    )
    mirrored = Step.from_yaml(
        {
            "label": "AMD mirror",
            "device": "h200_18gb",
            "commands": ["pytest tests/example.py"],
            "mirror": {
                "amd": {
                    "device": "mi300_1",
                    "hf_offline_retry": value,
                }
            },
        }
    )

    assert direct.hf_offline_retry is value
    assert mirrored.mirror["amd"]["hf_offline_retry"] is value


@pytest.mark.parametrize(
    "yaml_data",
    [
        {
            "label": "Invalid direct AMD policy",
            "hf_offline_retry": "sometimes",
        },
        {
            "label": "Invalid AMD mirror policy",
            "mirror": {
                "amd": {
                    "device": "mi300_1",
                    "hf_offline_retry": "sometimes",
                }
            },
        },
    ],
)
def test_hf_offline_retry_rejects_non_boolean_values(yaml_data):
    with pytest.raises(ValueError, match="valid boolean"):
        Step.from_yaml(yaml_data)


def _rocm_base_refresh_step():
    return Step(
        label="AMD: :docker: refresh ROCm base",
        group="Hardware - AMD Build",
        key=amd.AMD_ROCM_BASE_REFRESH_STEP_KEY,
        device="amd_cpu",
        no_plugin=True,
        commands=["bash .buildkite/scripts/rocm/refresh-base-image.sh"],
    )


@pytest.mark.parametrize(
    ("list_file_diff", "expected_timeout"),
    [
        ([], 15),
        (["vllm/config.py"], 15),
        ([amd.AMD_ROCM_BASE_DOCKERFILE], 540),
    ],
)
def test_rocm_base_refresh_timeout_tracks_dockerfile_change(
    fake_global_config, list_file_diff, expected_timeout
):
    fake_global_config["list_file_diff"] = list_file_diff

    command_step = _render_single_step(_rocm_base_refresh_step()).steps[0]

    assert command_step.timeout_in_minutes == expected_timeout


def test_rocm_base_refresh_force_uses_build_timeout(monkeypatch):
    monkeypatch.setenv("ROCM_BASE_REFRESH_FORCE", "1")

    command_step = _render_single_step(_rocm_base_refresh_step()).steps[0]

    assert command_step.timeout_in_minutes == 540


def test_skip_timeout_omits_rocm_base_refresh_timeout(fake_global_config, monkeypatch):
    monkeypatch.setenv(buildkite_step.SKIP_TIMEOUT_ENV_VAR, "1")
    fake_global_config["list_file_diff"] = [amd.AMD_ROCM_BASE_DOCKERFILE]

    command_step = _render_single_step(_rocm_base_refresh_step()).steps[0]

    assert command_step.timeout_in_minutes is None


@pytest.mark.parametrize(
    ("device", "queue", "dind", "expected_gpu_count"),
    [
        ("mi300_4", AgentQueue.AMD_MI300_4, False, "4"),
        ("mi300_4", AgentQueue.AMD_MI300_4, True, "4"),
        ("mi325_1", AgentQueue.AMD_MI325_1, False, "1"),
        ("mi325_1", AgentQueue.AMD_MI325_1, True, "1"),
    ],
)
def test_direct_amd_gpu_steps_use_dind_flag(device, queue, dind, expected_gpu_count):
    step = Step(
        label="AMD direct test",
        group="Direct AMD",
        key=f"amd-direct-{device}",
        depends_on=["image-build"],
        device=device,
        dind=dind,
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
    if not dind:
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

    assert command_step.env[amd.AMD_HF_OFFLINE_RETRY_ENV] == "1"
    assert command_step.retry == amd.get_amd_retry(True)
    assert len(command_step.retry["automatic"]) == 5
    assert 1 not in _retry_exit_statuses(command_step)
    assert command_step.retry["automatic"][0] == {
        "signal_reason": "stack_error",
        "limit": 1,
    }
    assert len(amd.AMD_RETRY["automatic"]) == 6
    assert any(rule.get("exit_status") == 1 for rule in amd.AMD_RETRY["automatic"])

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


@pytest.mark.parametrize(
    ("hf_offline_retry", "input_value", "expected_value", "retries_status_one"),
    [
        (True, "0", "1", False),
        (False, "1", "0", True),
    ],
)
def test_direct_amd_hf_offline_retry_is_authoritative(
    hf_offline_retry,
    input_value,
    expected_value,
    retries_status_one,
):
    step = Step(
        label="AMD retry policy",
        group="Direct AMD",
        device="mi300_1",
        commands=["pytest tests/example.py"],
        env={amd.AMD_HF_OFFLINE_RETRY_ENV: input_value},
        hf_offline_retry=hf_offline_retry,
    )

    group_step = _render_single_step(step)
    command_step = next(
        item
        for item in group_step.steps
        if isinstance(item, buildkite_step.BuildkiteCommandStep)
    )

    assert command_step.env[amd.AMD_HF_OFFLINE_RETRY_ENV] == expected_value
    assert (1 in _retry_exit_statuses(command_step)) is retries_status_one


def test_no_plugin_amd_step_bypasses_hf_offline_retry():
    step = Step(
        label="AMD direct command",
        group="Direct AMD",
        device="mi300_1",
        commands=["bash tests/standalone_tests/example.sh"],
        no_plugin=True,
    )

    group_step = _render_single_step(step)
    command_step = next(
        item
        for item in group_step.steps
        if isinstance(item, buildkite_step.BuildkiteCommandStep)
    )

    assert command_step.commands != [amd.AMD_TEST_COMMAND]
    assert command_step.env is None
    assert 1 in _retry_exit_statuses(command_step)


def test_multi_node_amd_step_gets_authoritative_hf_offline_retry_opt_out():
    step = Step(
        label="AMD multi-node test",
        group="Direct AMD",
        device="mi300_1",
        num_devices=1,
        num_nodes=2,
        commands=["pytest tests/distributed/example.py"],
    )

    group_step = _render_single_step(step)
    command_step = next(
        item
        for item in group_step.steps
        if isinstance(item, buildkite_step.BuildkiteCommandStep)
    )

    assert command_step.commands == [amd.AMD_TEST_COMMAND]
    assert command_step.env[amd.AMD_HF_OFFLINE_RETRY_ENV] == "0"
    assert 1 in _retry_exit_statuses(command_step)


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
        "export HSA_TOOLS_LIB=/opt/rocm/lib/librocm-debug-agent.so.2" in test_commands
    )
    assert "HSA_ENABLE_DEBUG=1" in test_commands
    assert "ROCm debug agent enabled" in test_commands
    assert "WARNING: ROCm debug agent not found at" in test_commands


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

    group_steps = buildkite_step.convert_group_step_to_buildkite_step(
        {
            step.group: [step],
        }
    )
    default_group = next(group for group in group_steps if group.group == "Mirrors")
    default_command_step = next(
        s
        for s in default_group.steps
        if isinstance(s, buildkite_step.BuildkiteCommandStep)
    )
    amd_group = next(
        group for group in group_steps if group.group == "Hardware-AMD Tests"
    )
    amd_command_step = next(
        s for s in amd_group.steps if isinstance(s, buildkite_step.BuildkiteCommandStep)
    )

    assert default_command_step.depends_on == ["image-build"]
    assert default_command_step.soft_fail is False
    assert len(amd_group.steps) == 1
    assert amd_command_step.depends_on == ["image-build-amd"]
    assert amd_command_step.agents == {"queue": AgentQueue.AMD_MI325_1}
    assert amd_command_step.soft_fail is True
    assert amd_command_step.env[amd.AMD_HF_OFFLINE_RETRY_ENV] == "1"
    assert 1 not in _retry_exit_statuses(amd_command_step)
    assert "ROCm debug agent disabled" in (amd_command_step.env["VLLM_TEST_COMMANDS"])


def test_amd_mirror_can_opt_out_of_hf_offline_retry(fake_global_config):
    fake_global_config["list_file_diff"] = ["vllm/foo.py"]
    step = Step(
        label="Mirrored retry opt-out",
        group="Mirrors",
        commands=["pytest tests/mirror.py"],
        source_file_dependencies=["vllm/"],
        device="h200_18gb",
        mirror={
            "amd": {
                "device": "mi325_1",
                "hf_offline_retry": False,
            }
        },
    )

    group_steps = buildkite_step.convert_group_step_to_buildkite_step(
        {
            step.group: [step],
        }
    )
    default_group = next(group for group in group_steps if group.group == "Mirrors")
    default_command_step = next(
        item
        for item in default_group.steps
        if isinstance(item, buildkite_step.BuildkiteCommandStep)
    )
    amd_group = next(
        group for group in group_steps if group.group == "Hardware-AMD Tests"
    )
    amd_command_step = next(
        item
        for item in amd_group.steps
        if isinstance(item, buildkite_step.BuildkiteCommandStep)
    )

    assert not default_command_step.env
    assert amd_command_step.env[amd.AMD_HF_OFFLINE_RETRY_ENV] == "0"
    assert 1 in _retry_exit_statuses(amd_command_step)


def test_dind_false_mirror_uses_native_runner_gating(fake_global_config):
    fake_global_config["list_file_diff"] = [
        ".buildkite/scripts/hardware_ci/run-amd-test.sh"
    ]
    step = Step(
        label="Native mirrored test",
        group="Mirrors",
        commands=["pytest tests/mirror.py"],
        source_file_dependencies=["vllm/"],
        device="h200_18gb",
        mirror={"amd": {"device": "mi325_1", "dind": False}},
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
    assert amd_command_step.env[amd.AMD_HF_OFFLINE_RETRY_ENV] == "1"


def test_untagged_mirror_defaults_to_dind(
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
    assert amd_command_step.env[amd.AMD_HF_OFFLINE_RETRY_ENV] == "1"


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
        s
        for s in group_step.steps
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
        s
        for s in group_step.steps
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

    group_steps = buildkite_step.convert_group_step_to_buildkite_step(
        {
            step.group: [step],
        }
    )
    default_group = next(group for group in group_steps if group.group == "Mirrors")
    default_command_step = next(
        s
        for s in default_group.steps
        if isinstance(s, buildkite_step.BuildkiteCommandStep)
    )
    amd_group = next(
        group for group in group_steps if group.group == "Hardware-AMD Tests"
    )
    amd_command_step = next(
        s for s in amd_group.steps if isinstance(s, buildkite_step.BuildkiteCommandStep)
    )

    # The main step keeps its own timeout; the AMD mirror uses the (larger)
    # timeout declared on the mirror block.
    assert default_command_step.timeout_in_minutes == 40
    assert amd_command_step.timeout_in_minutes == 75


def test_skip_timeout_omits_main_and_amd_mirror_timeouts(
    monkeypatch,
    fake_global_config,
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

    group_steps = buildkite_step.convert_group_step_to_buildkite_step(
        {
            step.group: [step],
        }
    )
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

    group_steps = buildkite_step.convert_group_step_to_buildkite_step(
        {
            step.group: [step],
        }
    )
    amd_group = next(
        group for group in group_steps if group.group == "Hardware-AMD Tests"
    )
    amd_command_step = next(
        s for s in amd_group.steps if isinstance(s, buildkite_step.BuildkiteCommandStep)
    )

    # An AMD mirror without its own timeout must not inherit the shorter main
    # timeout (AMD runs slower); it stays unbounded until one is declared.
    assert amd_command_step.timeout_in_minutes is None
