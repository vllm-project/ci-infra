import pytest

import buildkite_step
from constants import AgentQueue
from step import Step
from utils_lib import docker_utils

pytestmark = pytest.mark.usefixtures("fake_global_config")


def _render_intel_step(num_devices, **kwargs):
    step = Step(
        label=f"Intel K8s test x{num_devices}",
        group="Intel K8s tests",
        device="intel_gpu_k8s",
        num_devices=num_devices,
        commands=["cd tests", "pytest -v -s v1/cudagraph"],
        **kwargs,
    )
    group_step = buildkite_step.convert_group_step_to_buildkite_step(
        {step.group: [step]}
    )[0]
    return next(
        s
        for s in group_step.steps
        if isinstance(s, buildkite_step.BuildkiteCommandStep)
    )


@pytest.mark.parametrize(
    ("num_devices", "cpu_request", "cpu_limit", "memory_request", "shm"),
    [
        (1, "8", "32", "32Gi", "8Gi"),
        (4, "32", "128", "128Gi", "32Gi"),
    ],
)
def test_intel_k8s_step_uses_workspace_image_and_per_gpu_resources(
    monkeypatch, fake_global_config, num_devices, cpu_request, cpu_limit,
    memory_request, shm
):
    fake_global_config["torch_nightly"] = "1"
    monkeypatch.setattr(docker_utils, "get_global_config", lambda: fake_global_config)
    monkeypatch.setattr(buildkite_step, "get_image", docker_utils.get_image)

    step = _render_intel_step(num_devices=num_devices)

    assert step.agents == {"queue": AgentQueue.INTEL_GPU_K8S}
    assert len(step.plugins) == 1
    k8s = step.plugins[0]["kubernetes"]
    pod_spec = k8s["podSpec"]

    assert "initContainers" not in pod_spec
    assert pod_spec["resourceClaims"] == [
        {"name": "gpu", "resourceClaimTemplateName": f"b70-{num_devices}"}
    ]
    assert pod_spec["volumes"][0]["emptyDir"] == {
        "medium": "Memory",
        "sizeLimit": shm,
    }

    container = pod_spec["containers"][0]
    assert (
        container["image"]
        == "example.com/vllm/vllm-ci-test-repo:$BUILDKITE_COMMIT-xpu"
    )
    assert "workingDir" not in container
    assert "cd tests" in step.commands
    env = {entry["name"]: entry for entry in container["env"]}
    assert "PYTHONPATH" not in env
    assert env["HF_TOKEN"]["valueFrom"]["secretKeyRef"] == {
        "name": "hf-token-secret",
        "key": "token",
    }
    assert container["resources"]["requests"] == {
        "cpu": cpu_request,
        "memory": memory_request,
    }
    assert container["resources"]["limits"] == {
        "cpu": cpu_limit,
        "memory": f"{64 * num_devices}Gi",
    }


def test_intel_k8s_step_does_not_inject_nvidia_or_recording_setup(monkeypatch):
    monkeypatch.setattr(buildkite_step, "kernrec_enabled", lambda: True)
    monkeypatch.setattr(buildkite_step, "fnrec_enabled", lambda: True)

    step = _render_intel_step(num_devices=1, timeout_in_minutes=30)

    assert step.timeout_in_minutes == 30
    commands = "\n".join(step.commands)
    assert "nvidia-smi" not in commands
    assert "CUDA_ENABLE_COREDUMP" not in commands
    assert "ci_setup.sh" not in commands
    assert "kernrec" not in commands.lower()
    assert "fnrec" not in commands.lower()
    assert step.artifact_paths is None
