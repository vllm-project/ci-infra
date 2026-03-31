import sys
import os

# Add the pipeline_generator directory to the path so imports resolve correctly.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from plugin.docker_plugin import get_docker_plugin, _COMMON_ENV
from constants import DeviceType
from step import Step


def _make_step(**kwargs):
    defaults = {"label": "Test"}
    defaults.update(kwargs)
    return Step(**defaults)


class TestDefaultDevice:
    def test_gpus_all(self):
        plugin = get_docker_plugin(_make_step(device=DeviceType.H100), "img")
        assert plugin["gpus"] == "all"

    def test_environment_includes_hf_home_fsx(self):
        plugin = get_docker_plugin(_make_step(device=DeviceType.H100), "img")
        assert "HF_HOME=/fsx/hf_cache" in plugin["environment"]

    def test_environment_includes_ray_compat(self):
        plugin = get_docker_plugin(_make_step(device=DeviceType.H100), "img")
        assert "RAY_COMPAT_SLACK_WEBHOOK_URL" in plugin["environment"]

    def test_volumes(self):
        plugin = get_docker_plugin(_make_step(device=DeviceType.H100), "img")
        assert "/fsx/hf_cache:/fsx/hf_cache" in plugin["volumes"]
        assert "/dev/shm:/dev/shm" in plugin["volumes"]


class TestH200:
    def test_volumes(self):
        plugin = get_docker_plugin(_make_step(device=DeviceType.H200), "img")
        assert "/mnt/vllm-ci:/mnt/vllm-ci" in plugin["volumes"]
        assert "/dev/shm:/dev/shm" in plugin["volumes"]

    def test_gpus_all(self):
        plugin = get_docker_plugin(_make_step(device=DeviceType.H200), "img")
        assert plugin["gpus"] == "all"


class TestB200:
    def test_volumes(self):
        plugin = get_docker_plugin(_make_step(device=DeviceType.B200), "img")
        assert "/raid:/raid" in plugin["volumes"]
        assert "/mnt/shared:/mnt/shared" in plugin["volumes"]
        assert "/dev/shm:/dev/shm" in plugin["volumes"]

    def test_no_gpus_key(self):
        plugin = get_docker_plugin(_make_step(device=DeviceType.B200), "img")
        assert "gpus" not in plugin


class TestCPU:
    def test_gpus_removed(self):
        plugin = get_docker_plugin(_make_step(device=DeviceType.CPU), "img")
        assert "gpus" not in plugin

    def test_cpu_small_gpus_removed(self):
        plugin = get_docker_plugin(_make_step(device=DeviceType.CPU_SMALL), "img")
        assert "gpus" not in plugin

    def test_cpu_medium_gpus_removed(self):
        plugin = get_docker_plugin(_make_step(device=DeviceType.CPU_MEDIUM), "img")
        assert "gpus" not in plugin


class TestCommonEnv:
    def test_all_common_env_in_default(self):
        plugin = get_docker_plugin(_make_step(device=DeviceType.H100), "img")
        for var in _COMMON_ENV:
            assert var in plugin["environment"]

    def test_all_common_env_in_h200(self):
        plugin = get_docker_plugin(_make_step(device=DeviceType.H200), "img")
        for var in _COMMON_ENV:
            assert var in plugin["environment"]

    def test_all_common_env_in_b200(self):
        plugin = get_docker_plugin(_make_step(device=DeviceType.B200), "img")
        for var in _COMMON_ENV:
            assert var in plugin["environment"]


class TestMountBuildkiteAgent:
    def test_benchmarks_label(self):
        plugin = get_docker_plugin(_make_step(label="Benchmarks"), "img")
        assert plugin["mount_buildkite_agent"] is True

    def test_mount_flag(self):
        plugin = get_docker_plugin(_make_step(mount_buildkite_agent=True), "img")
        assert plugin["mount_buildkite_agent"] is True

    def test_no_mount_by_default(self):
        plugin = get_docker_plugin(_make_step(), "img")
        assert "mount_buildkite_agent" not in plugin


class TestNoSharedMutation:
    def test_two_calls_return_independent_dicts(self):
        p1 = get_docker_plugin(_make_step(device=DeviceType.H100), "img1")
        p2 = get_docker_plugin(_make_step(device=DeviceType.H100), "img2")
        p1["environment"].append("EXTRA=1")
        assert "EXTRA=1" not in p2["environment"]
        assert p1["image"] == "img1"
        assert p2["image"] == "img2"
