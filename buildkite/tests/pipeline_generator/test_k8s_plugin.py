import os
import sys
from pathlib import Path

# Add plugin dir to path
plugin_dir = Path(__file__).resolve().parent.parent.parent / "pipeline_generator" / "plugin"
sys.path.insert(0, str(plugin_dir))
sys.path.insert(0, str(plugin_dir.parent))

from k8s_plugin import get_k8s_plugin
from step import Step
from constants import DeviceType


def test_k8s_plugin_pr_isolation(monkeypatch):
    step = Step(label="test", device=DeviceType.H100)
    
    # Non-PR build
    monkeypatch.setenv("BUILDKITE_PULL_REQUEST", "false")
    plugin_main = get_k8s_plugin(step, "test-image")
    volumes_main = plugin_main["kubernetes"]["podSpec"]["volumes"]
    hf_vol_main = next(v for v in volumes_main if v["name"] == "hf-cache")
    assert hf_vol_main["hostPath"]["path"] == "/mnt/hf-cache"

    # PR build
    monkeypatch.setenv("BUILDKITE_PULL_REQUEST", "123")
    plugin_pr = get_k8s_plugin(step, "test-image")
    volumes_pr = plugin_pr["kubernetes"]["podSpec"]["volumes"]
    hf_vol_pr = next(v for v in volumes_pr if v["name"] == "hf-cache")
    assert hf_vol_pr["hostPath"]["path"] == "/mnt/hf-cache-pr"
