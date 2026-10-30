from pathlib import Path

import pytest

import amd
import buildkite_step
from amd import is_amd_device
from constants import AgentQueue
from step import Step

pytestmark = pytest.mark.usefixtures("fake_global_config")


def test_amd_template_retries_gpu_hang_abort():
    template = (Path(__file__).parents[2] / "test-template-amd.j2").read_text()

    assert (
        "{{ indent }}    - exit_status: 134  # ROCm/KFD GPU hang (SIGABRT)\n"
        "{{ indent }}      limit: 1"
    ) in template


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
        label=":docker: Refresh ROCm base",
        group="Hardware - AMD Build",
        key=amd.AMD_ROCM_BASE_REFRESH_STEP_KEY,
        device="amd_cpu",
        no_plugin=True,
        commands=["bash .buildkite/scripts/rocm/refresh-base-image.sh"],
        concurrency=1,
        concurrency_group="vllm/rocm/base-build/$BUILDKITE_COMMIT",
    )


@pytest.mark.parametrize("branch", ["main", "feature"])
def test_amd_cpu_docker_step_stays_on_the_amd_cpu_queue(fake_global_config, branch):
    """A ":docker:" label must not pull an amd_cpu step onto the NVIDIA CPU
    queues; the device decides."""
    fake_global_config["branch"] = branch

    assert buildkite_step.get_agent_queue(_rocm_base_refresh_step()) == (
        AgentQueue.AMD_CPU
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


@pytest.fixture
def build_sh_layout(tmp_path, monkeypatch):
    """A vLLM checkout that builds its ROCm images with build.sh."""
    (tmp_path / ".buildkite/scripts/rocm").mkdir(parents=True)
    (tmp_path / ".buildkite/scripts/rocm/build.sh").touch()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BUILDKITE_REPO", "https://github.com/vllm-project/vllm.git")
    monkeypatch.setenv("BUILDKITE_PULL_REQUEST", "false")
    monkeypatch.setenv("BUILDKITE_BRANCH", "main")
    monkeypatch.delenv("AITER_NIGHTLY", raising=False)
    return monkeypatch


@pytest.mark.parametrize(
    "env,expected",
    [
        ({}, "rocm/vllm-ci"),
        ({"BUILDKITE_PULL_REQUEST": "42"}, "rocm/vllm-dev"),
        ({"BUILDKITE_REPO": "git@github.com:someone/vllm.git"}, "rocm/vllm-dev"),
        ({"AITER_NIGHTLY": "1"}, "rocm/vllm-dev"),
        ({"BUILDKITE_BRANCH": "releases/v0.99"}, "rocm/vllm-dev"),
    ],
)
def test_build_sh_layout_build_steps_get_trusted_repo(build_sh_layout, env, expected):
    for name, value in env.items():
        build_sh_layout.setenv(name, value)
    build_sh_layout.setenv("ROCM_BASE_REFRESH_SKIP", "1")  # ignored by build.sh

    step = _rocm_base_refresh_step().model_copy(
        update={"concurrency_group": "g/$ROCM_CI_REPO/$BUILDKITE_BRANCH"}
    )

    command_step = _render_single_step(step).steps[0]

    assert command_step.env == {"ROCM_CI_REPO": expected}
    assert command_step.concurrency_group == f"g/{expected}/$BUILDKITE_BRANCH"
    assert command_step.timeout_in_minutes == 540
    assert command_step.agents == {"queue": AgentQueue.AMD_CPU}


def test_build_sh_layout_pods_run_the_build_image(build_sh_layout):
    build_sh_layout.setenv("BUILDKITE_PULL_REQUEST", "42")
    step = Step(
        label="AMD test",
        group="Direct AMD",
        key="amd-test",
        device="mi300_1",
        dind=False,
        commands=["pytest tests/foo.py"],
    )

    command_step = _render_single_step(step).steps[-1]

    assert command_step.depends_on[0] == "image-build-amd"
    pod = command_step.plugins[0]["kubernetes"]["podSpecPatch"]
    container = pod["containers"][0]
    assert container["image"] == "rocm/vllm-dev:build-$BUILDKITE_BUILD_ID"
    assert command_step.env["VLLM_CI_BASE_IMAGE"] == container["image"]
    assert container["imagePullPolicy"] == "Always"
    assert [m["name"] for m in container["volumeMounts"]] == ["devshm"]
    assert [v["name"] for v in pod["volumes"]] == ["devshm"]


def test_amd_cpu_step_gets_only_the_repo_wide_retries():
    step = Step(
        label="AMD build",
        group="Hardware - AMD Build",
        key="amd-build-no-yaml-retry",
        device="amd_cpu",
        commands=["build"],
    )

    command_step = _render_single_step(step).steps[-1]

    assert command_step.retry == {
        "automatic": [
            {"signal_reason": "stack_error", "limit": 1},
            {"exit_status": -1, "limit": 1},
            {"exit_status": 255, "limit": 1},
            {"exit_status": 94, "limit": 1},
            {"exit_status": [201, 202, 203, 204, 205], "limit": 1},
        ]
    }


@pytest.mark.parametrize(
    ("device", "queue", "dind", "expected_gpu_count"),
    [
        ("mi300_4", AgentQueue.AMD_MI300_4, False, "4"),
        ("mi300_4", AgentQueue.AMD_MI300_4, True, "4"),
        ("mi325_1", AgentQueue.AMD_MI325_1, False, "1"),
        ("mi325_1", AgentQueue.AMD_MI325_1, True, "1"),
        ("mi355_dpx", AgentQueue.AMD_MI355_DPX, False, "1"),
        ("mi355_dpx", AgentQueue.AMD_MI355_DPX, True, "1"),
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
    (command_step,) = group_step.steps

    assert group_step.group == "Hardware-AMD Tests"
    # Optional, so it waits in the "Run more jobs" input step.
    assert command_step.deferred
    tag = {"mi300_4": "MI300", "mi325_1": "MI325", "mi355_dpx": "MI355 DPX"}[device]
    assert command_step.label == f":amd: ({tag}) AMD direct test"
    assert command_step.depends_on == ["image-build-amd"]
    assert command_step.agents == {"queue": queue}
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
        if device == "mi355_dpx":
            assert "resources" not in container
        else:
            assert container["resources"]["limits"]["amd.com/gpu"] == expected_gpu_count
            assert (
                container["resources"]["requests"]["amd.com/gpu"] == expected_gpu_count
            )
        assert command_step.env["AMD_CI_RUNTIME"] == "native"
        assert "DOCKER_IMAGE_NAME" not in command_step.env
        container_env = {entry["name"]: entry for entry in container["env"]}
        assert (
            container_env["VLLM_CI_EXPECTED_GPU_COUNT"]["value"] == expected_gpu_count
        )
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

    # AMD's own retries plus the repo-wide infra ones (255, 94, 201-205).
    assert command_step.retry == buildkite_step.ensure_infra_failure_retry(
        amd.AMD_RETRY
    )
    automatic = command_step.retry["automatic"]
    assert {"exit_status": 94, "limit": 1} in automatic
    assert {"exit_status": 255, "limit": 1} in automatic
    assert {"exit_status": [201, 202, 203, 204, 205], "limit": 1} in automatic
    assert {"signal_reason": "stack_error", "limit": 1} in automatic
    assert {"exit_status": 134, "limit": 1} in automatic

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


@pytest.mark.parametrize("device,gpu_count", [("mi300_4", 4), ("mi355_dpx", 1)])
def test_amd_device_rejects_conflicting_gpu_count(device, gpu_count):
    step = Step(
        label="AMD GPU count mismatch",
        group="Direct AMD",
        device=device,
        num_devices=2,
        commands=["pytest tests/example.py"],
    )

    with pytest.raises(
        ValueError,
        match=rf"AMD device {device} provides {gpu_count} GPUs, but num_devices=2",
    ):
        _render_single_step(step)


@pytest.mark.parametrize(
    "device,gpu_size",
    [("mi250_1", "1"), ("mi250_2", "2"), ("mi250_4", "4"), ("mi250_8", "8")],
)
def test_mi250_steps_share_one_queue_split_by_gpu_size(device, gpu_size):
    step = Step(
        label="MI250 test",
        group="Direct AMD",
        key=f"amd-{device}",
        device=device,
        dind=False,
        agent_tags={"gpu_size": "99", "queue": "other", "label": "x"},
        commands=["pytest tests/foo.py"],
    )

    command_step = _render_single_step(step).steps[-1]

    assert command_step.agents == {
        "queue": AgentQueue.AMD_MI250,
        "gpu_size": gpu_size,
        "label": "x",
    }
    assert command_step.env["VLLM_CI_EXPECTED_GPU_COUNT"] == gpu_size


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
    (command_step,) = group_step.steps

    test_commands = command_step.env["VLLM_TEST_COMMANDS"]
    assert "if test -f /opt/rocm/lib/librocm-debug-agent.so.2" in test_commands
    assert (
        "export HSA_TOOLS_LIB=/opt/rocm/lib/librocm-debug-agent.so.2" in test_commands
    )
    assert "HSA_ENABLE_DEBUG=1" in test_commands
    assert "ROCm debug agent enabled" in test_commands
    assert "WARNING: ROCm debug agent not found at" in test_commands


@pytest.mark.parametrize(
    "device,queue",
    [
        ("mi325_1", AgentQueue.AMD_MI325_1),
        ("mi355_dpx", AgentQueue.AMD_MI355_DPX),
    ],
)
def test_amd_mirror_uses_shared_gating_with_amd_dependency_fallback(
    fake_global_config,
    device,
    queue,
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
                "device": device,
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
    assert default_command_step.key == "mirrored-test"
    assert default_command_step.soft_fail is False
    assert len(amd_group.steps) == 1
    assert amd_command_step.key == "amd-mirrored-test"
    assert amd_command_step.depends_on == ["image-build-amd"]
    assert amd_command_step.agents == {"queue": queue}
    assert amd_command_step.soft_fail is False
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

    assert default_group.steps[0].deferred
    assert len(amd_group.steps) == 1
    assert not amd_group.steps[0].deferred
    assert isinstance(amd_group.steps[0], buildkite_step.BuildkiteCommandStep)


@pytest.mark.parametrize(
    "device,queue",
    [
        ("mi325_1", AgentQueue.AMD_MI325_1),
        ("mi355_dpx", AgentQueue.AMD_MI355_DPX),
    ],
)
def test_dind_false_mirror_uses_native_runner_gating(fake_global_config, device, queue):
    fake_global_config["list_file_diff"] = [
        ".buildkite/scripts/hardware_ci/run-amd-test.sh"
    ]
    step = Step(
        label="Native mirrored test",
        group="Mirrors",
        commands=["pytest tests/mirror.py"],
        source_file_dependencies=["vllm/"],
        device="h200_18gb",
        mirror={"amd": {"device": device, "dind": False}},
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

    assert default_group.steps[0].deferred
    assert len(amd_group.steps) == 1
    assert not amd_group.steps[0].deferred
    amd_command_step = amd_group.steps[0]
    assert isinstance(amd_command_step, buildkite_step.BuildkiteCommandStep)
    assert amd_command_step.key == "amd-native-mirrored-test"
    assert amd_command_step.agents == {"queue": queue}
    assert amd_command_step.env["VLLM_CI_EXPECTED_GPU_COUNT"] == "1"
    assert amd_command_step.plugins is not None
    pod_patch = amd_command_step.plugins[0]["kubernetes"]["podSpecPatch"]
    container = pod_patch["containers"][0]
    if device == "mi355_dpx":
        assert "resources" not in container
    else:
        assert container["resources"] == {
            "limits": {"amd.com/gpu": "1"},
            "requests": {"amd.com/gpu": "1"},
        }


@pytest.mark.parametrize("device", ["mi300_4", "mi355_dpx"])
def test_native_amd_no_gpu_preserves_allocation_contract(device):
    step = Step(
        label="Native AMD CPU test",
        group="Direct AMD",
        device=device,
        dind=False,
        no_gpu=True,
        commands=["pytest tests/cpu.py"],
    )

    command_step = next(
        s
        for s in _render_single_step(step).steps
        if isinstance(s, buildkite_step.BuildkiteCommandStep)
    )

    assert command_step.env["VLLM_CI_EXPECTED_GPU_COUNT"] == "0"
    container = command_step.plugins[0]["kubernetes"]["podSpecPatch"]["containers"][0]
    container_env = {entry["name"]: entry for entry in container["env"]}
    assert container_env["VLLM_CI_EXPECTED_GPU_COUNT"]["value"] == "0"
    if device == "mi355_dpx":
        assert "resources" not in container
    else:
        assert container["resources"] == {
            "limits": {"amd.com/gpu": "0"},
            "requests": {"amd.com/gpu": "0"},
        }


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

    (amd_command_step,) = amd_group.steps
    assert amd_command_step.deferred
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
    # A mirror gets the same retries as a direct AMD step.
    assert amd_command_step.retry == buildkite_step.ensure_infra_failure_retry(
        amd.AMD_RETRY
    )


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
    "label,device,expected",
    [
        ("Basic Correctness", "mi250_1", ":amd: (MI250) Basic Correctness"),
        (
            ":nvidia: (H200) Basic Correctness",
            "mi300_1",
            ":amd: (MI300) Basic Correctness",
        ),
        (
            ":computer: (CPU) Docker Build Metadata",
            "mi325_8",
            ":amd: (MI325) Docker Build Metadata",
        ),
        ("LM Eval Small Models", "mi355_dpx", ":amd: (MI355 DPX) LM Eval Small Models"),
        (":amd: (MI355) Fusion E2E TP2", "mi355_2", ":amd: (MI355) Fusion E2E TP2"),
        (":docker: Refresh ROCm base", "amd_cpu", ":docker: Refresh ROCm base"),
    ],
)
def test_amd_label_reads_amd_tag_title(label, device, expected):
    assert amd.get_amd_label(label, device) == expected


@pytest.mark.parametrize(
    "amd_label,expected_amd_label",
    [
        (None, ":amd: (MI300) Mirrored label test"),
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

    # A mirror-level label is used verbatim; without one the label reads
    # ":amd: (<tag>) <label>". The NVIDIA label is never affected either way.
    assert amd_command_step.label == expected_amd_label
    assert default_command_step.label == "Mirrored label test"


@pytest.mark.parametrize(
    ("amd_parallelism", "expected"),
    [
        (None, 4),  # unset -> inherit parent parallelism (existing behavior)
        (1, 1),  # explicit override -> AMD stays a single copy
        (2, 2),  # explicit override -> independent AMD shard count
    ],
)
def test_amd_mirror_parallelism_override(fake_global_config, amd_parallelism, expected):
    """NVIDIA sharding must not multiply AMD mirrors: mirror.amd.parallelism
    overrides the inherited parent parallelism when set."""
    amd_config = {
        "device": "mi325_1",
        "depends_on": ["image-build-amd"],
        # pooling-shaped mirror: custom unsharded AMD command preserved
        "commands": ["pytest tests/pooling.py"],
    }
    if amd_parallelism is not None:
        amd_config["parallelism"] = amd_parallelism
    step = Step(
        label="Pooling test",
        group="Models",
        key="pooling-test",
        depends_on=["image-build"],
        working_dir="/vllm-workspace/tests",
        commands=[
            "pytest tests/pooling.py --shard-id=$$BUILDKITE_PARALLEL_JOB"
            " --num-shards=$$BUILDKITE_PARALLEL_JOB_COUNT"
        ],
        parallelism=4,
        source_file_dependencies=["vllm/"],
        mirror={"amd": amd_config},
    )

    group_steps = buildkite_step.convert_group_step_to_buildkite_step(
        {step.group: [step]}
    )
    nvidia_group = next(g for g in group_steps if g.group == "Models")
    nvidia_step = next(
        s
        for s in nvidia_group.steps
        if isinstance(s, buildkite_step.BuildkiteCommandStep)
    )
    amd_group = next(g for g in group_steps if g.group == "Hardware-AMD Tests")
    amd_step = next(
        s for s in amd_group.steps if isinstance(s, buildkite_step.BuildkiteCommandStep)
    )

    # parent sharding always preserved
    assert nvidia_step.parallelism == 4
    assert amd_step.parallelism == expected
    # custom AMD command remains the unsharded original
    assert "pytest tests/pooling.py" in amd_step.env["VLLM_TEST_COMMANDS"]
    assert "--num-shards" not in amd_step.env["VLLM_TEST_COMMANDS"]


@pytest.mark.parametrize(
    ("device", "expected"),
    [
        ("amd_cpu", True),
        ("mi300_2", True),
        ("mi355_8", True),
        ("h100", False),
        (None, False),
    ],
)
def test_is_amd_device(device, expected):
    assert is_amd_device(device) is expected
