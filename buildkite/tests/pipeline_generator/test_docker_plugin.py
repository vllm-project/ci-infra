import os
import sys
from pathlib import Path

plugin_dir = Path(__file__).resolve().parent.parent.parent / "pipeline_generator" / "plugin"
sys.path.insert(0, str(plugin_dir))
sys.path.insert(0, str(plugin_dir.parent))

from docker_plugin import get_docker_plugin
from step import Step
from constants import DeviceType


def test_docker_plugin_volume_pr_isolation(monkeypatch):
    step = Step(label="test", device=DeviceType.CPU)

    # Non-PR build
    monkeypatch.setenv("BUILDKITE_PULL_REQUEST", "false")
    plugin_main = get_docker_plugin(step, "test-image")
    assert "/fsx/hf_cache:/fsx/hf_cache" in plugin_main["volumes"]

    # PR build
    monkeypatch.setenv("BUILDKITE_PULL_REQUEST", "456")
    plugin_pr = get_docker_plugin(step, "test-image")
    assert "/fsx/hf_cache_pr:/fsx/hf_cache" in plugin_pr["volumes"]
