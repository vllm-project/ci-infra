from pathlib import Path

import pytest

import amd
import buildkite_step
from constants import AgentQueue
from step import Step

pytestmark = pytest.mark.usefixtures("fake_global_config")


def test_stable_image_promotion_capability_uses_build_scoped_dind_base(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    promotion_script = tmp_path / "promote-stable-images.sh"
    promotion_script.touch()
    monkeypatch.setattr(amd, "AMD_ROCM_PROMOTION_SCRIPT", promotion_script)

    assert not amd.supports_stable_image_promotion()
    assert amd.get_amd_ci_base_image(dind=False) == amd.AMD_NATIVE_BASE_IMAGE
    assert amd.get_amd_ci_base_image(dind=True) == amd.AMD_STABLE_CI_BASE_IMAGE

    producer = tmp_path / ".buildkite/scripts/ci-bake-rocm.sh"
    producer.parent.mkdir(parents=True)
    producer.write_text("CI_BASE_IMAGE_TAG_BUILD_REF=enabled")
    monkeypatch.setattr(amd, "AMD_ROCM_BASE_PRODUCER", producer)

    assert amd.supports_stable_image_promotion()
    assert amd.get_amd_ci_base_image(dind=False) == amd.AMD_NATIVE_BASE_IMAGE
    assert amd.get_amd_ci_base_image(dind=True) == amd.AMD_NATIVE_BASE_IMAGE


def test_amd_template_retries_gpu_hang_abort():
    template = (Path(__file__).parents[2] / "test-template-amd.j2").read_text()

    assert (
        "{{ indent }}    - exit_status: 134  # ROCm/KFD GPU hang (SIGABRT)\n"
        "{{ indent }}      limit: 1"
    ) in template
    assert "exit_status: 2  # Exact image found but registry handoff failed" in template


def test_amd_template_and_bootstrap_preserve_promotion_contract():
    template = (Path(__file__).parents[2] / "test-template-amd.j2").read_text()
    bootstrap = (Path(__file__).parents[2] / "bootstrap-amd.sh").read_text()

    assert "ci_base-build-$BUILDKITE_BUILD_ID" in template
    assert 'key: "promote-stable-rocm-images-amd"' in template
    assert "if: build.branch == pipeline.default_branch" in template
    assert 'concurrency_group: "vllm/rocm/stable-image-promotion"' in template
    assert 'IMAGE_TAG: "rocm/vllm-ci:build-$BUILDKITE_BUILD_ID"' in template
    assert "IMAGE_TAG_LATEST:" not in template
    assert 'VLLM_CI_SMOKE_IMAGE: "rocm/vllm-ci:build-$BUILDKITE_BUILD_ID"' in template
    assert (
        'VLLM_CI_FALLBACK_IMAGE: "rocm/vllm-ci:build-$BUILDKITE_BUILD_ID"' in template
    )
    assert 'REMOTE_VLLM: "0"' in template
    assert 'REMOTE_VLLM: "1"' not in template
    assert '[[ -f ".buildkite/scripts/rocm/promote-stable-images.sh" ]]' in bootstrap
    promotion_gate = bootstrap.split("local rocm_stable_image_promotion=0", 1)[1]
    promotion_gate = promotion_gate.split("rocm_stable_image_promotion=1", 1)[0]
    assert "CI_BASE_IMAGE_TAG_BUILD_REF" in promotion_gate
    assert "-D rocm_build_scoped_images=" not in bootstrap
    assert "-D rocm_stable_image_promotion=" in bootstrap


def test_amd_template_uses_build_scoped_images():
    template = (Path(__file__).parents[2] / "test-template-amd.j2").read_text()

    assert amd.AMD_NATIVE_BASE_IMAGE in template
    assert amd.AMD_CI_IMAGE in template
    assert "rocm_build_scoped_images" not in template


def test_amd_template_configures_gpu_diagnostics():
    template = (Path(__file__).parents[2] / "test-template-amd.j2").read_text()

    assert 'VLLM_CI_DIAGNOSTICS_DIR: "artifacts/amd-gpu-diagnostics"' in template
    assert 'BUILDKITE_ARTIFACT_UPLOAD_SKIP_SYMLINKS: "true"' in template
    assert (
        "        {% else %}\n"
        "        command: bash .buildkite/scripts/hardware_ci/run-amd-test.sh\n"
        "        artifact_paths:\n"
        '          - "artifacts/amd-gpu-diagnostics/*/diagnostics.log"\n'
        "        env:"
    ) in template
    for name, field_path in amd.AMD_NATIVE_POD_IDENTITY_ENV.items():
        assert f"- name: {name}" in template
        assert f"fieldPath: {field_path}" in template


def _render_single_step(step):
    return buildkite_step.convert_group_step_to_buildkite_step(
        {
            step.group: [step],
        }
    )[0]


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


@pytest.mark.parametrize(
    "list_file_diff",
    [
        [],
        ["vllm/config.py"],
        ["run_all"],
        ["nightly"],
        ["docker/Dockerfile.rocm_base"],
    ],
)
def test_rocm_base_selector_keeps_build_timeout(fake_global_config, list_file_diff):
    fake_global_config["list_file_diff"] = list_file_diff

    command_step = _render_single_step(_rocm_base_refresh_step()).steps[0]

    assert command_step.timeout_in_minutes == 540
    assert command_step.env["ROCM_BASE_REFRESH_FORCE"] == "0"
    assert command_step.concurrency == 1
    assert command_step.concurrency_group.endswith("/$BUILDKITE_COMMIT")


def test_rocm_stable_promotion_always_runs(fake_global_config):
    fake_global_config["list_file_diff"] = ["vllm/config.py"]
    step = Step(
        label="AMD: promote stable ROCm images",
        group="Hardware - AMD Build",
        key=amd.AMD_STABLE_IMAGE_PROMOTION_STEP_KEY,
        device="amd_cpu",
        no_plugin=True,
        commands=["promote"],
    )

    assert buildkite_step._step_should_run(step, fake_global_config["list_file_diff"])


def test_rocm_base_refresh_force_continues_to_pass_through(monkeypatch):
    monkeypatch.setenv("ROCM_BASE_REFRESH_FORCE", "1")

    command_step = _render_single_step(_rocm_base_refresh_step()).steps[0]

    assert command_step.env["ROCM_BASE_REFRESH_FORCE"] == "1"
    assert command_step.timeout_in_minutes == 540


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
        concurrency=2,
        concurrency_group="vllm/test/direct-amd",
        if_condition="build.branch == pipeline.default_branch",
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
    assert command_step.concurrency == 2
    assert command_step.concurrency_group == "vllm/test/direct-amd"
    assert command_step.if_condition == "build.branch == pipeline.default_branch"
    assert command_step.commands == [
        "bash .buildkite/scripts/hardware_ci/run-amd-test.sh",
    ]
    assert command_step.artifact_paths == [amd.AMD_DIAGNOSTICS_ARTIFACT_GLOB]
    assert command_step.env["BUILDKITE_ARTIFACT_UPLOAD_SKIP_SYMLINKS"] == "true"
    assert command_step.model_dump(exclude_none=True)["artifact_paths"] == (
        command_step.artifact_paths
    )
    assert command_step.env["VLLM_CI_DIAGNOSTICS_DIR"] == amd.AMD_DIAGNOSTICS_DIR
    assert command_step.env["VLLM_CI_EXPECTED_GPU_COUNT"] == expected_gpu_count
    if not dind:
        assert command_step.plugins is not None
        pod_patch = command_step.plugins[0]["kubernetes"]["podSpecPatch"]
        container = pod_patch["containers"][0]
        assert container["image"] == amd.AMD_NATIVE_BASE_IMAGE
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
        assert command_step.env["VLLM_CI_FALLBACK_IMAGE"] == amd.AMD_CI_IMAGE

    assert command_step.env["VLLM_CI_BASE_IMAGE"] == (
        amd.AMD_NATIVE_BASE_IMAGE if not dind else amd.AMD_STABLE_CI_BASE_IMAGE
    )

    assert command_step.retry == amd.AMD_RETRY
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
        concurrency=2,
        concurrency_group="vllm/test/mirrored",
        if_condition="build.branch == pipeline.default_branch",
        mirror={
            "amd": {
                "device": "mi325_1",
                "depends_on": ["image-build-amd"],
                "soft_fail": False,
                "source_file_dependencies": ["amd-only/"],
                "concurrency": 1,
                "concurrency_group": "vllm/test/amd-mirrored",
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
    assert default_command_step.concurrency == 2
    assert default_command_step.concurrency_group == "vllm/test/mirrored"
    assert (
        default_command_step.if_condition == "build.branch == pipeline.default_branch"
    )
    assert len(amd_group.steps) == 1
    assert amd_command_step.key == "amd-mirrored-test"
    assert amd_command_step.depends_on == ["image-build-amd"]
    assert amd_command_step.agents == {"queue": AgentQueue.AMD_MI325_1}
    assert amd_command_step.soft_fail is False
    assert amd_command_step.concurrency == 1
    assert amd_command_step.concurrency_group == "vllm/test/amd-mirrored"
    assert amd_command_step.if_condition == "build.branch == pipeline.default_branch"
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
    assert command_step.artifact_paths == (
        None if no_plugin else [amd.AMD_DIAGNOSTICS_ARTIFACT_GLOB]
    )
    serialized_step = command_step.model_dump(exclude_none=True)
    if no_plugin:
        assert "artifact_paths" not in serialized_step
    else:
        assert serialized_step["artifact_paths"] == command_step.artifact_paths


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


@pytest.mark.parametrize(
    "amd_label,expected_amd_label",
    [
        (None, "AMD: Mirrored label test (mi300_1)"),
        (
            ":amd: MI300 Attention Kernels",
            ":amd: MI300 Attention Kernels",
        ),
    ],
)
def test_amd_mirror_label_override(fake_global_config, amd_label, expected_amd_label):
    fake_global_config["list_file_diff"] = ["vllm/foo.py"]
    amd_mirror = {
        "device": "mi300_1",
        "depends_on": ["image-build-amd"],
    }
    if amd_label is not None:
        amd_mirror["label"] = amd_label
    step = Step(
        label="Mirrored label test",
        group="Mirrors",
        key="mirrored-label",
        depends_on=["image-build"],
        working_dir="/vllm-workspace/tests",
        commands=["pytest tests/mirror.py"],
        source_file_dependencies=["vllm/"],
        device="h200_18gb",
        mirror={"amd": amd_mirror},
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
    default_group = next(group for group in group_steps if group.group == "Mirrors")
    default_command_step = next(
        s
        for s in default_group.steps
        if isinstance(s, buildkite_step.BuildkiteCommandStep)
    )

    # A mirror-level label is used verbatim; without one the derived
    # "AMD: <label> (<device>)" form is unchanged. The NVIDIA label is
    # never affected either way.
    assert amd_command_step.label == expected_amd_label
    assert default_command_step.label == "Mirrored label test"
