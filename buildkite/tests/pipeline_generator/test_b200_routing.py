import sys
from pathlib import Path


PIPELINE_GENERATOR_DIR = Path(__file__).resolve().parents[2] / "pipeline_generator"
sys.path.insert(0, str(PIPELINE_GENERATOR_DIR))

import buildkite_step
from constants import AgentQueue
from step import Step


def test_b200_uses_docker_queue_and_plugin(monkeypatch):
    monkeypatch.setattr(buildkite_step, "get_global_config", lambda: {"branch": "feature"})
    monkeypatch.setattr(buildkite_step, "get_image", lambda *args: "public.ecr.aws/test/image:tag")

    step = Step(label="B200 test", commands=["true"], device="b200")

    assert buildkite_step.get_agent_queue(step) == AgentQueue.B200

    plugin = buildkite_step._get_step_plugin(step)
    assert "docker#v5.2.0" in plugin
    assert plugin["docker#v5.2.0"]["image"] == "public.ecr.aws/test/image:tag"
    assert "/raid:/raid" in plugin["docker#v5.2.0"]["volumes"]


def test_b200_k8s_uses_kubernetes_queue_and_plugin(monkeypatch):
    monkeypatch.setattr(buildkite_step, "get_global_config", lambda: {"branch": "feature"})
    monkeypatch.setattr(buildkite_step, "get_image", lambda *args: "public.ecr.aws/test/image:tag")

    step = Step(label="B200 k8s test", commands=["true"], device="b200-k8s", num_devices=2)

    assert buildkite_step.get_agent_queue(step) == AgentQueue.B200_K8S

    plugin = buildkite_step._get_step_plugin(step)
    pod_spec = plugin["kubernetes"]["podSpec"]
    container = pod_spec["containers"][0]

    assert container["image"] == (
        "936637512419.dkr.ecr.us-west-2.amazonaws.com/vllm-ci-pull-through-cache"
        "/test/image:tag"
    )
    assert container["resources"]["limits"]["nvidia.com/gpu"] == 2
    assert pod_spec["runtimeClassName"] == "nvidia"


def test_b200_nightly_uses_docker_plugin():
    step = Step(label="B200 nightly test", commands=["true"], device="b200")

    plugin = buildkite_step._get_nightly_step_plugin(step, "public.ecr.aws/test/nightly:tag")

    assert "docker#v5.2.0" in plugin
    assert plugin["docker#v5.2.0"]["image"] == "public.ecr.aws/test/nightly:tag"


def test_b200_k8s_nightly_uses_kubernetes_plugin():
    step = Step(label="B200 k8s nightly test", commands=["true"], device="b200-k8s")

    plugin = buildkite_step._get_nightly_step_plugin(step, "public.ecr.aws/test/nightly:tag")

    assert "kubernetes" in plugin
    assert plugin["kubernetes"]["podSpec"]["containers"][0]["image"] == (
        "936637512419.dkr.ecr.us-west-2.amazonaws.com/vllm-ci-pull-through-cache"
        "/test/nightly:tag"
    )
