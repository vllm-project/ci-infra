import sys
from pathlib import Path

import pytest

PIPELINE_GENERATOR_DIR = Path(__file__).resolve().parents[2] / "pipeline_generator"
sys.path.insert(0, str(PIPELINE_GENERATOR_DIR))

import buildkite_step
import step as step_module
from constants import AgentQueue
from step import Step


@pytest.fixture(autouse=True)
def fake_global_config(monkeypatch):
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
    # Make sure the EKS gates never leak in from the ambient environment.
    monkeypatch.delenv("GPU_1_K8S", raising=False)
    monkeypatch.delenv("GPU_4_K8S", raising=False)
    return config


def _render_command_step(step):
    group_step = buildkite_step.convert_group_step_to_buildkite_step({
        step.group: [step],
    })[0]
    return next(
        s
        for s in group_step.steps
        if isinstance(s, buildkite_step.BuildkiteCommandStep)
    )


def _make_l4_step(num_devices, **kwargs):
    return Step(
        label=f"L4 test x{num_devices}",
        group="L4 tests",
        device="l4",
        num_devices=num_devices,
        commands=["pytest tests/foo.py"],
        **kwargs,
    )


def test_gated_4gpu_l4_step_uses_k8s_plugin(monkeypatch):
    monkeypatch.setenv("GPU_4_K8S", "1")
    command_step = _render_command_step(_make_l4_step(num_devices=4))

    assert command_step.agents == {"queue": AgentQueue.L4_K8S}
    assert command_step.agents["queue"] == "l4-k8s"
    assert command_step.retry == buildkite_step.K8S_RETRY

    assert len(command_step.plugins) == 1
    plugin = command_step.plugins[0]
    assert set(plugin.keys()) == {"kubernetes"}

    k8s = plugin["kubernetes"]
    annotations = k8s["metadata"]["annotations"]
    assert annotations["cluster-autoscaler.kubernetes.io/safe-to-evict"] == "false"

    pod_spec = k8s["podSpec"]
    assert pod_spec["nodeSelector"] == {"vllm.ci/gpu-pool": "l4x4"}
    assert {
        "key": "nvidia.com/gpu",
        "operator": "Exists",
        "effect": "NoSchedule",
    } in pod_spec["tolerations"]

    container = pod_spec["containers"][0]
    assert container["image"] == "test-image"
    assert container["resources"]["limits"]["nvidia.com/gpu"] == 4
    assert container["resources"]["requests"]["nvidia.com/gpu"] == 4
    assert container["resources"]["requests"]["cpu"] == "40"
    assert container["resources"]["requests"]["memory"] == "160Gi"

    volumes = {volume["name"]: volume for volume in pod_spec["volumes"]}
    assert volumes["devshm"]["emptyDir"] == {
        "medium": "Memory",
        "sizeLimit": "96Gi",
    }
    assert volumes["hf-cache"]["hostPath"] == {
        "path": "/fsx/hf_cache",
        "type": "DirectoryOrCreate",
    }

    env = {entry["name"]: entry for entry in container["env"]}
    assert env["HF_TOKEN"]["valueFrom"]["secretKeyRef"] == {
        "name": "hf-token-secret",
        "key": "token",
    }
    assert env["BUILDKITE_ANALYTICS_TOKEN"]["valueFrom"]["secretKeyRef"] == {
        "name": "buildkite-analytics-token",
        "key": "token",
        "optional": True,
    }


def test_gated_1gpu_l4_step_uses_k8s_plugin(monkeypatch):
    monkeypatch.setenv("GPU_1_K8S", "1")
    command_step = _render_command_step(_make_l4_step(num_devices=1))

    assert command_step.agents == {"queue": AgentQueue.L4_K8S}
    assert command_step.retry == buildkite_step.K8S_RETRY

    container = command_step.plugins[0]["kubernetes"]["podSpec"]["containers"][0]
    assert container["resources"]["limits"]["nvidia.com/gpu"] == 1
    assert container["resources"]["requests"]["nvidia.com/gpu"] == 1
    assert container["resources"]["requests"]["cpu"] == "10"
    assert container["resources"]["requests"]["memory"] == "40Gi"

    volumes = {
        volume["name"]: volume
        for volume in command_step.plugins[0]["kubernetes"]["podSpec"]["volumes"]
    }
    assert volumes["devshm"]["emptyDir"]["sizeLimit"] == "24Gi"


def test_gpu_1_gate_does_not_route_4gpu_step(monkeypatch):
    monkeypatch.setenv("GPU_1_K8S", "1")
    command_step = _render_command_step(_make_l4_step(num_devices=4))

    assert command_step.agents == {"queue": AgentQueue.GPU_4}
    assert "docker#v5.2.0" in command_step.plugins[0]
    assert command_step.retry is None


def test_gpu_4_gate_does_not_route_1gpu_step(monkeypatch):
    monkeypatch.setenv("GPU_4_K8S", "1")
    command_step = _render_command_step(_make_l4_step(num_devices=1))

    assert command_step.agents == {"queue": AgentQueue.GPU_1}
    assert "docker#v5.2.0" in command_step.plugins[0]
    assert command_step.retry is None


def test_ungated_l4_steps_keep_existing_ec2_routing():
    command_step_4gpu = _render_command_step(_make_l4_step(num_devices=4))
    assert command_step_4gpu.agents == {"queue": AgentQueue.GPU_4}
    assert "docker#v5.2.0" in command_step_4gpu.plugins[0]
    assert command_step_4gpu.retry is None

    command_step_1gpu = _render_command_step(_make_l4_step(num_devices=1))
    assert command_step_1gpu.agents == {"queue": AgentQueue.GPU_1}
    assert "docker#v5.2.0" in command_step_1gpu.plugins[0]
    assert command_step_1gpu.retry is None


def test_gated_l4_step_keeps_explicit_retry(monkeypatch):
    monkeypatch.setenv("GPU_4_K8S", "1")
    explicit_retry = {"automatic": [{"exit_status": 5, "limit": 3}]}
    command_step = _render_command_step(
        _make_l4_step(num_devices=4, retry=explicit_retry)
    )

    assert command_step.agents == {"queue": AgentQueue.L4_K8S}
    assert command_step.retry == explicit_retry


if __name__ == "__main__":
    sys.exit(pytest.main(["-v", __file__]))
