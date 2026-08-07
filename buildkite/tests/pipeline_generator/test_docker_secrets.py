import os
import sys
from pathlib import Path

plugin_dir = Path(__file__).resolve().parent.parent.parent / "pipeline_generator" / "plugin"
sys.path.insert(0, str(plugin_dir))
sys.path.insert(0, str(plugin_dir.parent))

from docker_plugin import get_docker_plugin
from step import Step
from constants import DeviceType


def test_docker_plugin_secrets_propagation(monkeypatch):
    step = Step(label="test", device=DeviceType.CPU)

    # Non-PR build
    monkeypatch.setenv("BUILDKITE_PULL_REQUEST", "false")
    plugin_main = get_docker_plugin(step, "test-image")
    assert plugin_main["propagate-environment"] is True
    assert "HF_TOKEN" in plugin_main["environment"]

    # PR build
    monkeypatch.setenv("BUILDKITE_PULL_REQUEST", "789")
    plugin_pr = get_docker_plugin(step, "test-image")
    assert plugin_pr["propagate-environment"] is False
    assert "HF_TOKEN" not in plugin_pr["environment"]
    assert "CODECOV_TOKEN" not in plugin_pr["environment"]
