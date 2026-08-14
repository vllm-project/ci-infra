from pathlib import Path

import pytest

import amd
import buildkite_step
from constants import AgentQueue
from step import Step

pytestmark = pytest.mark.usefixtures("fake_global_config")


def test_build_scoped_image_capability(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    assert amd.get_amd_native_base_image() == amd.AMD_LEGACY_NATIVE_BASE_IMAGE
    assert amd.get_amd_ci_image() == amd.AMD_LEGACY_CI_IMAGE

    producer = tmp_path / ".buildkite/scripts/ci-bake-rocm.sh"
    producer.parent.mkdir(parents=True)
    producer.write_text("CI_BASE_IMAGE_TAG_BUILD_REF=enabled")

    assert amd.get_amd_native_base_image() == amd.AMD_BUILD_NATIVE_BASE_IMAGE
    assert amd.get_amd_ci_image() == amd.AMD_BUILD_CI_IMAGE


def test_legacy_amd_template_retries_gpu_hang_abort():
    template = (Path(__file__).parents[2] / "test-template-amd.j2").read_text()

    assert (
        "{{ indent }}    - exit_status: 134  # ROCm/KFD GPU hang (SIGABRT)\n"
        "{{ indent }}      limit: 1"
    ) in template


def test_legacy_amd_template_configures_gpu_diagnostics():
    template = (Path(__file__).parents[2] / "test-template-amd.j2").read_text()

    assert 'VLLM_CI_DIAGNOSTICS_DIR: "artifacts/amd-gpu-diagnostics"' in template
    for name, field_path in amd.AMD_NATIVE_POD_IDENTITY_ENV.items():
        assert f"- name: {name}" in template
        assert f"fieldPath: {field_path}" in template


def _render_single_step(step):
    return buildkite_step.convert_group_step_to_buildkite_step(
        {
            step.group: [step],
        }
    )[0]


def _assert_exact_amd_retry(command_step):
    assert command_step.retry == amd.AMD_RETRY
    assert command_step.retry is not amd.AMD_RETRY
    assert command_step.retry["automatic"] is not amd.AMD_RETRY["automatic"]


def _render_command_step(step):
    return next(
        item
        for item in _render_single_step(step).steps
        if isinstance(item, buildkite_step.BuildkiteCommandStep)
    )


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


def test_hf_offline_retry_defaults_off_for_direct_and_mirrored_steps():
    direct = Step.from_yaml({"label": "Direct AMD"})
    mirrored = Step.from_yaml(
        {
            "label": "AMD mirror",
            "mirror": {"amd": {"device": "mi300_1"}},
        }
    )

    assert direct.hf_offline_retry is False
    assert "hf_offline_retry" not in mirrored.mirror["amd"]


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


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("no_plugin", "false"),
        ("no_plugin", None),
        ("dind", 1),
        ("num_nodes", "2"),
        ("num_nodes", True),
        ("num_devices", 0),
        ("concurrency", 0),
        ("concurrency", True),
        ("concurrency_group", ""),
        ("timeout_in_minutes", -1),
        ("device", 300),
        ("unsupported_option", True),
    ],
)
def test_amd_mirror_runtime_schema_rejects_malformed_fields(field, value):
    with pytest.raises(ValueError):
        Step.from_yaml(
            {
                "label": "Invalid AMD mirror",
                "mirror": {
                    "amd": {
                        "device": "mi300_1",
                        field: value,
                    }
                },
            }
        )


@pytest.mark.parametrize(
    "concurrency_options",
    [
        {"concurrency": 1},
        {"concurrency_group": "vllm/rocm/mirror"},
        {"concurrency": 1, "concurrency_group": "   "},
    ],
)
def test_amd_mirror_runtime_schema_rejects_invalid_concurrency_pairs(
    concurrency_options,
):
    with pytest.raises(ValueError):
        Step.from_yaml(
            {
                "label": "Invalid AMD mirror concurrency",
                "mirror": {
                    "amd": {
                        "device": "mi300_1",
                        **concurrency_options,
                    }
                },
            }
        )


@pytest.mark.parametrize(
    ("mirror_override", "expected_concurrency", "expected_group"),
    [
        ({"concurrency": 2}, 2, "vllm/rocm/parent"),
        ({"concurrency_group": "vllm/rocm/mirror"}, 1, "vllm/rocm/mirror"),
    ],
)
def test_amd_mirror_concurrency_can_override_one_parent_field(
    fake_global_config,
    mirror_override,
    expected_concurrency,
    expected_group,
):
    fake_global_config["list_file_diff"] = ["vllm/model_executor/foo.py"]
    step = Step(
        label="Mirrored concurrency override",
        group="Mirrors",
        commands=["pytest tests/mirror.py"],
        source_file_dependencies=["vllm/"],
        concurrency=1,
        concurrency_group="vllm/rocm/parent",
        mirror={"amd": {"device": "mi300_1", **mirror_override}},
    )

    group_steps = buildkite_step.convert_group_step_to_buildkite_step(
        {step.group: [step]}
    )
    amd_group = next(
        group for group in group_steps if group.group == "Hardware-AMD Tests"
    )
    command_step = next(
        item
        for item in amd_group.steps
        if isinstance(item, buildkite_step.BuildkiteCommandStep)
    )

    assert command_step.concurrency == expected_concurrency
    assert command_step.concurrency_group == expected_group


def _rocm_base_refresh_step():
    return Step(
        label="AMD: :docker: refresh ROCm base",
        group="Hardware - AMD Build",
        key=amd.AMD_ROCM_BASE_REFRESH_STEP_KEY,
        device="amd_cpu",
        no_plugin=True,
        commands=["bash .buildkite/scripts/rocm/refresh-base-image.sh"],
        concurrency=1,
        concurrency_group="vllm/rocm/base-build/$BUILDKITE_COMMIT",
    )


def test_rocm_base_selector_keeps_build_timeout():
    command_step = _render_single_step(_rocm_base_refresh_step()).steps[0]

    assert command_step.timeout_in_minutes == 540
    assert command_step.concurrency == 1
    assert command_step.concurrency_group.endswith("/$BUILDKITE_COMMIT")


def test_skip_timeout_omits_rocm_base_refresh_timeout(fake_global_config, monkeypatch):
    monkeypatch.setenv(buildkite_step.SKIP_TIMEOUT_ENV_VAR, "1")
    fake_global_config["list_file_diff"] = ["docker/Dockerfile.rocm_base"]

    command_step = _render_single_step(_rocm_base_refresh_step()).steps[0]

    assert command_step.timeout_in_minutes is None
    assert "timeout_in_minutes" not in command_step.model_dump(exclude_none=True)


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
    assert command_step.env["VLLM_CI_DIAGNOSTICS_DIR"] == amd.AMD_DIAGNOSTICS_DIR
    assert command_step.env["VLLM_CI_EXPECTED_GPU_COUNT"] == expected_gpu_count
    if not dind:
        assert command_step.plugins is not None
        pod_patch = command_step.plugins[0]["kubernetes"]["podSpecPatch"]
        container = pod_patch["containers"][0]
        assert container["image"] == amd.get_amd_native_base_image()
        assert container["resources"]["limits"]["amd.com/gpu"] == expected_gpu_count
        assert container["resources"]["requests"]["amd.com/gpu"] == expected_gpu_count
        assert command_step.env["AMD_CI_RUNTIME"] == "native"
        assert "DOCKER_IMAGE_NAME" not in command_step.env
        container_env = {entry["name"]: entry for entry in container["env"]}
        for name, field_path in amd.AMD_NATIVE_POD_IDENTITY_ENV.items():
            assert container_env[name] == {
                "name": name,
                "valueFrom": {"fieldRef": {"fieldPath": field_path}},
            }
    else:
        assert command_step.plugins is None
        assert "AMD_CI_RUNTIME" not in command_step.env
        assert command_step.env["DOCKER_IMAGE_NAME"] == amd.AMD_STABLE_CI_BASE_IMAGE
        assert command_step.env["VLLM_CI_FALLBACK_IMAGE"] == amd.get_amd_ci_image()

    assert command_step.env["VLLM_CI_BASE_IMAGE"] == (
        amd.get_amd_native_base_image() if not dind else amd.AMD_STABLE_CI_BASE_IMAGE
    )

    assert command_step.env[amd.AMD_HF_OFFLINE_RETRY_ENV] == "0"
    _assert_exact_amd_retry(command_step)
    assert len(command_step.retry["automatic"]) == 7
    assert command_step.retry["automatic"][0] == {
        "signal_reason": "stack_error",
        "limit": 1,
    }
    assert {"exit_status": 134, "limit": 1} in command_step.retry["automatic"]

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
    ("config_overrides", "step_request", "expected_value"),
    [
        pytest.param({"amd_hf_offline_retry": True}, True, "1", id="enabled"),
        pytest.param({}, True, "0", id="no-capability"),
        pytest.param({"amd_hf_offline_retry": True}, False, "0", id="no-request"),
        pytest.param(
            {"amd_hf_offline_retry": True, "nightly": "1"},
            True,
            "1",
            id="nightly",
        ),
        pytest.param(
            {"amd_hf_offline_retry": True, "torch_nightly": "1"},
            True,
            "1",
            id="torch-nightly",
        ),
        pytest.param(
            {
                "amd_hf_offline_retry": True,
                "disable_hf_offline_retry": True,
            },
            True,
            "0",
            id="kill-switch",
        ),
    ],
)
def test_direct_amd_hf_offline_retry_is_authoritative(
    fake_global_config,
    config_overrides,
    step_request,
    expected_value,
):
    fake_global_config.update(config_overrides)
    step = Step(
        label="AMD retry policy",
        group="Direct AMD",
        device="mi300_1",
        commands=["pytest tests/example.py"],
        env={amd.AMD_HF_OFFLINE_RETRY_ENV: "0" if expected_value == "1" else "1"},
        hf_offline_retry=step_request,
    )

    command_step = _render_command_step(step)

    assert command_step.env[amd.AMD_HF_OFFLINE_RETRY_ENV] == expected_value
    _assert_exact_amd_retry(command_step)


@pytest.mark.parametrize(
    ("step_overrides", "uses_wrapper"),
    [
        pytest.param(
            {
                "commands": ["bash tests/standalone_tests/example.sh"],
                "no_plugin": True,
            },
            False,
            id="direct-command",
        ),
        pytest.param(
            {
                "commands": ["pytest tests/distributed/example.py"],
                "num_devices": 1,
                "num_nodes": 2,
            },
            True,
            id="multi-node",
        ),
    ],
)
def test_ineligible_amd_jobs_do_not_enable_hf_offline_retry(
    fake_global_config, step_overrides, uses_wrapper
):
    fake_global_config["amd_hf_offline_retry"] = True
    step = Step(
        label="Ineligible AMD retry policy",
        group="Direct AMD",
        device="mi300_1",
        hf_offline_retry=True,
        **step_overrides,
    )

    command_step = _render_command_step(step)

    assert (command_step.commands == [amd.AMD_TEST_COMMAND]) is uses_wrapper
    if uses_wrapper:
        assert command_step.env[amd.AMD_HF_OFFLINE_RETRY_ENV] == "0"
    else:
        assert command_step.env is None
    _assert_exact_amd_retry(command_step)


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
    fake_global_config["amd_hf_offline_retry"] = True
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
                "concurrency": 2,
                "concurrency_group": "vllm/rocm/mirror",
                "source_file_dependencies": ["amd-only/"],
                "hf_offline_retry": True,
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
    assert default_command_step.key == "mirrored-test"
    assert default_command_step.soft_fail is False
    assert len(amd_group.steps) == 1
    assert amd_command_step.key == "amd-mirrored-test"
    assert amd_command_step.depends_on == ["image-build-amd"]
    assert amd_command_step.agents == {"queue": AgentQueue.AMD_MI325_1}
    assert amd_command_step.soft_fail is False
    assert amd_command_step.concurrency == 2
    assert amd_command_step.concurrency_group == "vllm/rocm/mirror"
    assert amd_command_step.env[amd.AMD_HF_OFFLINE_RETRY_ENV] == "1"
    _assert_exact_amd_retry(amd_command_step)
    assert "ROCm debug agent disabled" in (amd_command_step.env["VLLM_TEST_COMMANDS"])


@pytest.mark.parametrize("optional", [False, True])
def test_rocm_base_change_runs_only_amd_mirror(fake_global_config, optional):
    fake_global_config["list_file_diff"] = [amd.AMD_ROCM_BASE_DOCKERFILE]
    step = Step(
        label="ROCm base mirrored test",
        group="Mirrors",
        commands=["pytest tests/mirror.py"],
        source_file_dependencies=["vllm/"],
        optional=optional,
        mirror={"amd": {"device": "mi300_1"}},
    )

    group_steps = buildkite_step.convert_group_step_to_buildkite_step(
        {step.group: [step]}
    )
    default_group = next(group for group in group_steps if group.group == "Mirrors")
    amd_group = next(
        group for group in group_steps if group.group == "Hardware-AMD Tests"
    )

    assert isinstance(default_group.steps[0], buildkite_step.BuildkiteBlockStep)
    assert len(amd_group.steps) == 1
    assert isinstance(amd_group.steps[0], buildkite_step.BuildkiteCommandStep)


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
    assert amd_command_step.key == "amd-native-mirrored-test"
    assert amd_command_step.plugins is not None
    assert amd_command_step.env[amd.AMD_HF_OFFLINE_RETRY_ENV] == "0"
    _assert_exact_amd_retry(amd_command_step)


def test_untagged_mirror_defaults_to_dind(
    fake_global_config,
):
    fake_global_config["amd_hf_offline_retry"] = True
    fake_global_config["list_file_diff"] = [
        ".buildkite/scripts/hardware_ci/run-amd-test.sh"
    ]
    step = Step(
        label="DinD mirrored test",
        group="Mirrors",
        commands=["pytest tests/mirror.py"],
        source_file_dependencies=["vllm/"],
        device="h200_18gb",
        hf_offline_retry=True,
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
    # A mirror must opt in under mirror.amd; the parent step flag is not inherited.
    assert amd_command_step.env[amd.AMD_HF_OFFLINE_RETRY_ENV] == "0"
    _assert_exact_amd_retry(amd_command_step)


@pytest.mark.parametrize(
    ("requested_timeout", "expected_timeout"),
    [
        (None, 180),
        (90, 90),
        (540, 180),
    ],
)
def test_direct_amd_gpu_step_enforces_standard_timeout(
    requested_timeout, expected_timeout
):
    step = Step(
        label="AMD direct timed",
        group="Direct AMD",
        key="amd-direct-timed",
        depends_on=["image-build"],
        device="mi300_4",
        working_dir="/vllm-workspace/tests",
        commands=["pytest tests/foo.py"],
        timeout_in_minutes=requested_timeout,
    )

    group_step = _render_single_step(step)
    command_step = next(
        s
        for s in group_step.steps
        if isinstance(s, buildkite_step.BuildkiteCommandStep)
    )

    assert command_step.timeout_in_minutes == expected_timeout


@pytest.mark.parametrize("no_plugin", [False, True])
def test_skip_timeout_omits_direct_amd_timeout(monkeypatch, no_plugin):
    monkeypatch.setenv(buildkite_step.SKIP_TIMEOUT_ENV_VAR, "1")
    step = Step(
        label="AMD direct skipped timeout",
        group="Direct AMD",
        device="mi300_4",
        commands=["pytest tests/foo.py"],
        timeout_in_minutes=90,
        no_plugin=no_plugin,
    )

    group_step = _render_single_step(step)
    command_step = next(
        s
        for s in group_step.steps
        if isinstance(s, buildkite_step.BuildkiteCommandStep)
    )

    assert command_step.timeout_in_minutes is None
    assert "timeout_in_minutes" not in command_step.model_dump(exclude_none=True)


@pytest.mark.parametrize(
    ("mirror_timeout", "expected_timeout"),
    [
        (75, 75),
        (540, 180),
    ],
)
def test_amd_mirror_enforces_its_own_timeout(
    fake_global_config, mirror_timeout, expected_timeout
):
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
                "timeout_in_minutes": mirror_timeout,
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

    # The main step keeps its own timeout; the AMD mirror uses its declared
    # timeout up to the standard AMD limit.
    assert default_command_step.timeout_in_minutes == 40
    assert amd_command_step.timeout_in_minutes == expected_timeout


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


def test_amd_mirror_without_timeout_uses_standard_timeout(fake_global_config):
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

    # An AMD mirror without its own timeout does not inherit the shorter main
    # timeout (AMD runs slower); it receives the standard AMD timeout.
    assert amd_command_step.timeout_in_minutes == 180
