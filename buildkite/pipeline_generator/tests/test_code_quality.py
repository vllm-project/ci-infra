import sys
import os
import copy
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from step import Step
from constants import DeviceType
from buildkite_step import (
    _create_block_step,
    _generate_step_key,
    BuildkiteBlockStep,
)
from plugin.docker_plugin import get_docker_plugin, _COMMON_ENV
from plugin.k8s_plugin import get_k8s_plugin


# ---------------------------------------------------------------------------
# 1. _create_block_step() tests
# ---------------------------------------------------------------------------


class TestCreateBlockStep:
    def test_returns_buildkite_block_step(self):
        step = Step(label="My Test Step", device="h100")
        result = _create_block_step(step)
        assert isinstance(result, BuildkiteBlockStep)

    def test_block_field_format(self):
        step = Step(label="My Test Step", device="h100")
        result = _create_block_step(step)
        assert result.block == "Run My Test Step"

    def test_depends_on_is_empty_list(self):
        step = Step(label="example", device="cpu")
        result = _create_block_step(step)
        assert result.depends_on == []

    def test_key_format(self):
        step = Step(label="My Test Step", device="h100")
        result = _create_block_step(step)
        expected_key = "block-" + _generate_step_key("My Test Step")
        assert result.key == expected_key

    def test_no_crash_simple_label(self):
        """Verify function executes without errors (no leftover dead code)."""
        step = Step(label="simple", device="h100")
        result = _create_block_step(step)
        assert result.block == "Run simple"
        assert result.depends_on == []
        assert result.key == "block-simple"


# ---------------------------------------------------------------------------
# 2. _generate_step_key() tests
# ---------------------------------------------------------------------------


class TestGenerateStepKey:
    def test_basic_label(self):
        assert _generate_step_key("Basic Test") == "basic-test"

    def test_label_with_parentheses_and_comma(self):
        result = _generate_step_key("Test (gpu, 2)")
        # ( removed, ) removed, comma -> dash, space -> dash
        assert result == "test-gpu--2"

    def test_label_with_special_chars(self):
        result = _generate_step_key("Test: with/special.chars")
        # : -> -, / -> -, . -> -
        assert result == "test--with-special-chars"

    def test_percent_removed(self):
        result = _generate_step_key("Coverage 100%")
        assert result == "coverage-100"

    def test_plus_replaced(self):
        result = _generate_step_key("C++ Tests")
        assert result == "c---tests"

    def test_already_lowercase(self):
        result = _generate_step_key("already lowercase")
        assert result == "already-lowercase"

    def test_empty_string(self):
        result = _generate_step_key("")
        assert result == ""


# ---------------------------------------------------------------------------
# 3. Docker plugin tests
# ---------------------------------------------------------------------------


class TestGetDockerPlugin:
    def test_default_device_h100(self):
        step = Step(label="test", device=DeviceType.H100)
        plugin = get_docker_plugin(step, "test-image:latest")
        assert plugin["image"] == "test-image:latest"
        assert plugin["gpus"] == "all"
        assert plugin["always-pull"] is True
        assert plugin["propagate-environment"] is True

    def test_default_device_environment(self):
        step = Step(label="test", device=DeviceType.H100)
        plugin = get_docker_plugin(step, "img")
        env = plugin["environment"]
        assert "VLLM_USAGE_SOURCE=ci-test" in env
        assert "HF_TOKEN" in env
        assert "HF_HOME=/fsx/hf_cache" in env
        assert "NCCL_CUMEM_HOST_ENABLE=0" in env
        assert "CODECOV_TOKEN" in env
        assert "BUILDKITE_ANALYTICS_TOKEN" in env

    def test_h200_volumes(self):
        step = Step(label="test", device=DeviceType.H200)
        plugin = get_docker_plugin(step, "img")
        assert "/mnt/vllm-ci:/mnt/vllm-ci" in plugin["volumes"]
        assert "/dev/shm:/dev/shm" in plugin["volumes"]
        assert plugin["gpus"] == "all"

    def test_b200_volumes_and_no_gpus(self):
        step = Step(label="test", device=DeviceType.B200)
        plugin = get_docker_plugin(step, "img")
        assert "/raid:/raid" in plugin["volumes"]
        assert "/mnt/shared:/mnt/shared" in plugin["volumes"]
        assert "/dev/shm:/dev/shm" in plugin["volumes"]
        assert "gpus" not in plugin

    def test_cpu_device_no_gpus(self):
        step = Step(label="test", device=DeviceType.CPU)
        plugin = get_docker_plugin(step, "img")
        assert "gpus" not in plugin

    def test_cpu_small_device_no_gpus(self):
        step = Step(label="test", device=DeviceType.CPU_SMALL)
        plugin = get_docker_plugin(step, "img")
        assert "gpus" not in plugin

    def test_cpu_medium_device_no_gpus(self):
        step = Step(label="test", device=DeviceType.CPU_MEDIUM)
        plugin = get_docker_plugin(step, "img")
        assert "gpus" not in plugin

    def test_all_configs_include_common_env(self):
        """All device configs should include common environment variables."""
        for device in [DeviceType.H100, DeviceType.H200, DeviceType.B200]:
            step = Step(label="test", device=device)
            plugin = get_docker_plugin(step, "img")
            env = plugin["environment"]
            assert "VLLM_USAGE_SOURCE=ci-test" in env
            assert "HF_TOKEN" in env
            assert "NCCL_CUMEM_HOST_ENABLE=0" in env
            assert "CODECOV_TOKEN" in env
            assert "BUILDKITE_ANALYTICS_TOKEN" in env

    def test_benchmarks_step_mounts_buildkite_agent(self):
        step = Step(label="Benchmarks", device=DeviceType.H100)
        plugin = get_docker_plugin(step, "img")
        assert plugin["mount_buildkite_agent"] is True

    def test_mount_buildkite_agent_flag(self):
        step = Step(label="test", device=DeviceType.H100, mount_buildkite_agent=True)
        plugin = get_docker_plugin(step, "img")
        assert plugin["mount_buildkite_agent"] is True

    def test_non_benchmark_step_no_mount_agent(self):
        step = Step(label="test", device=DeviceType.H100)
        plugin = get_docker_plugin(step, "img")
        assert "mount_buildkite_agent" not in plugin


# ---------------------------------------------------------------------------
# 4. K8s plugin tests
# ---------------------------------------------------------------------------


class TestGetK8sPlugin:
    def test_h100_no_node_selector(self):
        step = Step(label="test", device=DeviceType.H100, num_devices=1)
        plugin = get_k8s_plugin(step, "public.ecr.aws/vllm:test")
        pod_spec = plugin["kubernetes"]["podSpec"]
        assert "nodeSelector" not in pod_spec

    def test_h100_no_priority_class(self):
        step = Step(label="test", device=DeviceType.H100, num_devices=1)
        plugin = get_k8s_plugin(step, "public.ecr.aws/vllm:test")
        pod_spec = plugin["kubernetes"]["podSpec"]
        assert "priorityClassName" not in pod_spec

    def test_h100_pull_through_cache_replacement(self):
        step = Step(label="test", device=DeviceType.H100, num_devices=1)
        plugin = get_k8s_plugin(step, "public.ecr.aws/vllm:test")
        image = plugin["kubernetes"]["podSpec"]["containers"][0]["image"]
        assert "public.ecr.aws" not in image
        assert "936637512419.dkr.ecr.us-west-2.amazonaws.com/vllm-ci-pull-through-cache" in image

    def test_a100_has_node_selector(self):
        step = Step(label="test", device=DeviceType.A100, num_devices=1)
        plugin = get_k8s_plugin(step, "img:test")
        pod_spec = plugin["kubernetes"]["podSpec"]
        assert pod_spec["nodeSelector"] == {
            "nvidia.com/gpu.product": "NVIDIA-A100-SXM4-80GB"
        }

    def test_a100_has_priority_class(self):
        step = Step(label="test", device=DeviceType.A100, num_devices=1)
        plugin = get_k8s_plugin(step, "img:test")
        pod_spec = plugin["kubernetes"]["podSpec"]
        assert pod_spec["priorityClassName"] == "ci"

    def test_h200_node_selector(self):
        step = Step(label="test", device=DeviceType.H200, num_devices=1)
        plugin = get_k8s_plugin(step, "img:test")
        pod_spec = plugin["kubernetes"]["podSpec"]
        assert pod_spec["nodeSelector"] == {
            "node.kubernetes.io/instance-type": "gpu-h200-sxm"
        }

    def test_h200_hardcoded_8_gpus(self):
        step = Step(label="test", device=DeviceType.H200, num_devices=1)
        plugin = get_k8s_plugin(step, "img:test")
        container = plugin["kubernetes"]["podSpec"]["containers"][0]
        assert container["resources"]["limits"]["nvidia.com/gpu"] == 8

    def test_unsupported_device_raises(self):
        step = Step(label="test", device=DeviceType.B200, num_devices=1)
        try:
            get_k8s_plugin(step, "img:test")
            assert False, "Expected ValueError"
        except ValueError as e:
            assert "Unsupported K8s device type" in str(e)

    def test_common_env_present(self):
        step = Step(label="test", device=DeviceType.H100, num_devices=1)
        plugin = get_k8s_plugin(step, "public.ecr.aws/img:test")
        container = plugin["kubernetes"]["podSpec"]["containers"][0]
        env = container["env"]
        env_names = [e["name"] for e in env]
        assert "VLLM_USAGE_SOURCE" in env_names
        assert "HF_TOKEN" in env_names
        assert "HF_HOME" in env_names
        assert "NCCL_CUMEM_HOST_ENABLE" in env_names
        # Check value of VLLM_USAGE_SOURCE
        for e in env:
            if e["name"] == "VLLM_USAGE_SOURCE":
                assert e["value"] == "ci-test"
            if e["name"] == "HF_TOKEN":
                assert "secretKeyRef" in e["valueFrom"]

    def test_num_devices_in_resource_limits(self):
        step = Step(label="test", device=DeviceType.H100, num_devices=4)
        plugin = get_k8s_plugin(step, "public.ecr.aws/img:test")
        container = plugin["kubernetes"]["podSpec"]["containers"][0]
        assert container["resources"]["limits"]["nvidia.com/gpu"] == 4

    def test_num_devices_default_1(self):
        step = Step(label="test", device=DeviceType.H100)
        plugin = get_k8s_plugin(step, "public.ecr.aws/img:test")
        container = plugin["kubernetes"]["podSpec"]["containers"][0]
        assert container["resources"]["limits"]["nvidia.com/gpu"] == 1


# ---------------------------------------------------------------------------
# 5. Plugin isolation tests
# ---------------------------------------------------------------------------


class TestPluginIsolation:
    def test_docker_plugin_independent_dicts(self):
        """Getting a plugin twice should return independent dicts."""
        step1 = Step(label="test1", device=DeviceType.H100)
        step2 = Step(label="test2", device=DeviceType.H100)
        plugin1 = get_docker_plugin(step1, "img1")
        plugin2 = get_docker_plugin(step2, "img2")

        # Mutate plugin1
        plugin1["environment"].append("EXTRA_VAR=1")
        plugin1["volumes"].append("/extra:/extra")

        # plugin2 should not be affected
        assert "EXTRA_VAR=1" not in plugin2["environment"]
        assert "/extra:/extra" not in plugin2["volumes"]

    def test_docker_plugin_different_images(self):
        step1 = Step(label="test1", device=DeviceType.H100)
        step2 = Step(label="test2", device=DeviceType.H100)
        plugin1 = get_docker_plugin(step1, "image-a")
        plugin2 = get_docker_plugin(step2, "image-b")
        assert plugin1["image"] == "image-a"
        assert plugin2["image"] == "image-b"

    def test_k8s_plugin_independent_dicts(self):
        """Getting a K8s plugin twice should return independent dicts."""
        step1 = Step(label="test1", device=DeviceType.H100, num_devices=1)
        step2 = Step(label="test2", device=DeviceType.H100, num_devices=2)
        plugin1 = get_k8s_plugin(step1, "public.ecr.aws/img1")
        plugin2 = get_k8s_plugin(step2, "public.ecr.aws/img2")

        # Mutate plugin1
        plugin1["kubernetes"]["podSpec"]["containers"][0]["env"].append(
            {"name": "EXTRA", "value": "1"}
        )

        # plugin2 should not be affected
        env_names2 = [
            e["name"]
            for e in plugin2["kubernetes"]["podSpec"]["containers"][0]["env"]
        ]
        assert "EXTRA" not in env_names2
