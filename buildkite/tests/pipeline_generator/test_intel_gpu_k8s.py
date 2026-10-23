import pytest

import buildkite_step
from constants import AgentQueue
from step import Step

pytestmark = pytest.mark.usefixtures("fake_global_config")


def _render_command_step(step):
    group_step = buildkite_step.convert_group_step_to_buildkite_step(
        {
            step.group: [step],
        }
    )[0]
    return next(
        s
        for s in group_step.steps
        if isinstance(s, buildkite_step.BuildkiteCommandStep)
    )


def _make_intel_step(num_devices, **kwargs):
    kwargs.setdefault("working_dir", "/workspace/vllm/tests")
    return Step(
        label=f":intel: (BMG) test x{num_devices}",
        group="Intel",
        device="intel_gpu_k8s",
        num_devices=num_devices,
        env={"VLLM_TEST_DEVICE": "xpu"},
        commands=["pytest -v -s 'foo' -k \"not bar\""],
        **kwargs,
    )


def test_1gpu_intel_step_uses_k8s_plugin():
    command_step = _render_command_step(_make_intel_step(num_devices=1))

    # Only the queue: agent_tags are for the bare-metal intel-gpu agents.
    assert command_step.agents == {"queue": AgentQueue.INTEL_GPU_K8S}
    assert command_step.agents["queue"] == "intel-gpu-k8s"
    assert command_step.env == {"VLLM_TEST_DEVICE": "xpu"}
    assert command_step.retry == {
        "automatic": [
            buildkite_step.EXIT_STATUS_NEGATIVE_ONE_RETRY,
            buildkite_step.EXIT_STATUS_255_RETRY,
        ]
    }

    assert len(command_step.plugins) == 1
    k8s = command_step.plugins[0]["kubernetes"]
    assert k8s["checkout"] == {"skip": True}

    pod_spec = k8s["podSpec"]
    assert pod_spec["resourceClaims"] == [
        {"name": "gpu", "resourceClaimTemplateName": "b70-1"}
    ]

    init = pod_spec["initContainers"][0]
    assert init["name"] == "copy-vllm-src"
    assert init["image"] == "test-image-xpu"
    assert init["command"] == ["cp", "-a", "/workspace/vllm", "/mnt/workspace/"]
    assert init["volumeMounts"] == [
        {"name": "workspace", "mountPath": "/mnt/workspace"}
    ]

    container = pod_spec["containers"][0]
    assert container["image"] == "test-image-xpu"
    assert container["resources"] == {
        "claims": [{"name": "gpu"}],
        "requests": {"cpu": "8", "memory": "32Gi"},
        "limits": {"cpu": "32", "memory": "64Gi"},
    }
    assert "nvidia.com/gpu" not in str(k8s)

    env = {entry["name"]: entry for entry in container["env"]}
    assert env["PYTHONPATH"]["value"] == ".."
    assert env["VLLM_DISABLE_COMPILE_CACHE"]["value"] == "1"
    assert env["HF_HOME"]["value"] == "/root/.cache/huggingface"
    assert env["HF_TOKEN"]["valueFrom"]["secretKeyRef"] == {
        "name": "hf-token-secret",
        "key": "token",
    }

    volumes = {volume["name"]: volume for volume in pod_spec["volumes"]}
    assert volumes["devshm"]["emptyDir"] == {"medium": "Memory", "sizeLimit": "8Gi"}
    assert volumes["hf-cache"]["hostPath"] == {
        "path": "/var/lib/hf-cache",
        "type": "DirectoryOrCreate",
    }


def test_intel_step_commands_have_no_nvidia_setup():
    command_step = _render_command_step(_make_intel_step(num_devices=1))

    assert command_step.commands[0] == "cd /workspace/vllm/tests"
    assert not any("nvidia-smi" in c for c in command_step.commands)
    assert not any("CUDA_" in c for c in command_step.commands)
    assert command_step.commands[-1] == 'pytest -v -s "foo" -k "not bar"'


@pytest.mark.parametrize(
    ("num_devices", "cpu", "memory", "cpu_limit", "memory_limit", "shm"),
    [
        (None, "8", "32Gi", "32", "64Gi", "8Gi"),
        (2, "16", "64Gi", "64", "128Gi", "16Gi"),
        (4, "32", "128Gi", "128", "256Gi", "32Gi"),
        (8, "64", "256Gi", "256", "512Gi", "64Gi"),
    ],
)
def test_intel_step_scales_with_num_devices(
    num_devices, cpu, memory, cpu_limit, memory_limit, shm
):
    command_step = _render_command_step(_make_intel_step(num_devices=num_devices))

    pod_spec = command_step.plugins[0]["kubernetes"]["podSpec"]
    claim = pod_spec["resourceClaims"][0]["resourceClaimTemplateName"]
    assert claim == f"b70-{num_devices or 1}"
    resources = pod_spec["containers"][0]["resources"]
    assert resources["requests"] == {"cpu": cpu, "memory": memory}
    assert resources["limits"] == {"cpu": cpu_limit, "memory": memory_limit}
    volumes = {volume["name"]: volume for volume in pod_spec["volumes"]}
    assert volumes["devshm"]["emptyDir"]["sizeLimit"] == shm


def test_intel_step_rejects_gpu_count_without_claim_template():
    with pytest.raises(ValueError, match="num_devices must be one of"):
        _render_command_step(_make_intel_step(num_devices=3))


def test_intel_step_keeps_parallelism():
    command_step = _render_command_step(_make_intel_step(num_devices=1, parallelism=4))

    assert command_step.parallelism == 4


def test_intel_step_skips_recorders(monkeypatch):
    monkeypatch.setattr(buildkite_step, "kernrec_enabled", lambda: True)
    monkeypatch.setattr(buildkite_step, "fnrec_enabled", lambda: True)

    command_step = _render_command_step(_make_intel_step(num_devices=1))

    assert command_step.artifact_paths is None
    assert not any(".kernrec" in c or "fnrec" in c for c in command_step.commands)


def test_bare_metal_intel_step_is_unchanged():
    step = Step(
        label=":intel: (BMG) bare metal",
        group="Intel",
        device="intel_gpu",
        agent_tags={"label": "production", "gpu": "1+"},
        no_plugin=True,
        commands=["bash .buildkite/scripts/hardware_ci/run-intel-test.sh 'true'"],
    )

    command_step = _render_command_step(step)

    assert command_step.agents == {
        "queue": AgentQueue.INTEL_GPU,
        "label": "production",
        "gpu": "1+",
    }
    assert command_step.plugins is None


if __name__ == "__main__":
    raise SystemExit(pytest.main(["-v", __file__]))
