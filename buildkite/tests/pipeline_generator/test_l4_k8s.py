import pytest

import buildkite_step
from constants import AgentQueue
from step import Step

pytestmark = pytest.mark.usefixtures("fake_global_config")


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


def test_4gpu_l4_step_uses_k8s_plugin():
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


def test_1gpu_l4_step_uses_k8s_plugin():
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


def test_l4_step_without_num_devices_defaults_to_one_gpu():
    command_step = _render_command_step(_make_l4_step(num_devices=None))

    assert command_step.agents == {"queue": AgentQueue.L4_K8S}
    container = command_step.plugins[0]["kubernetes"]["podSpec"]["containers"][0]
    assert container["resources"]["limits"]["nvidia.com/gpu"] == 1
    assert container["resources"]["requests"]["cpu"] == "10"


def test_l4_step_keeps_explicit_retry():
    explicit_retry = {"automatic": [{"exit_status": 5, "limit": 3}]}
    command_step = _render_command_step(
        _make_l4_step(num_devices=4, retry=explicit_retry)
    )

    assert command_step.agents == {"queue": AgentQueue.L4_K8S}
    # K8S_RETRY is not applied; the explicit retry only gets the repo-wide
    # exit-status -1 retry that main adds to every step.
    assert command_step.retry == buildkite_step.ensure_exit_status_negative_one_retry(
        explicit_retry
    )


@pytest.mark.parametrize(
    ("device", "num_devices", "queue"),
    [
        (None, None, AgentQueue.GPU_1),
        (None, 2, AgentQueue.GPU_4),
        (None, 4, AgentQueue.GPU_4),
        ("h100", None, AgentQueue.MITHRIL_H100),
        ("a100", None, AgentQueue.A100),
    ],
)
def test_non_l4_steps_keep_existing_routing(device, num_devices, queue):
    """Steps not marked device: l4 render exactly as before the migration."""
    step = Step(
        label="Regular GPU test",
        group="GPU tests",
        device=device,
        num_devices=num_devices,
        commands=["pytest tests/foo.py"],
    )

    command_step = _render_command_step(step)

    assert command_step.agents == {"queue": queue}
    # Every step gets the repo-wide exit-status -1 retry; non-l4 steps get
    # nothing beyond it (in particular not K8S_RETRY).
    assert command_step.retry == {
        "automatic": [buildkite_step.EXIT_STATUS_NEGATIVE_ONE_RETRY]
    }
    if device in ("h100", "a100"):
        # H100/A100 were already on the k8s plugin before this change.
        assert "kubernetes" in command_step.plugins[0]
    else:
        assert set(command_step.plugins[0].keys()) == {"docker#v5.2.0"}


if __name__ == "__main__":
    raise SystemExit(pytest.main(["-v", __file__]))
