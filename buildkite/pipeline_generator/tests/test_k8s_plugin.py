import sys
import os
import pytest

# Add pipeline_generator to sys.path so imports resolve correctly.
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), os.pardir)
)

from plugin.k8s_plugin import get_k8s_plugin, _COMMON_ENV
from step import Step
from constants import DeviceType


def _make_step(device, num_devices=None):
    return Step(label="test", device=device, num_devices=num_devices)


class TestH100Plugin:
    def test_no_node_selector(self):
        plugin = get_k8s_plugin(_make_step(DeviceType.H100), "public.ecr.aws/img:latest")
        pod_spec = plugin["kubernetes"]["podSpec"]
        assert "nodeSelector" not in pod_spec

    def test_no_priority_class(self):
        plugin = get_k8s_plugin(_make_step(DeviceType.H100), "public.ecr.aws/img:latest")
        pod_spec = plugin["kubernetes"]["podSpec"]
        assert "priorityClassName" not in pod_spec

    def test_image_pull_through_cache_replacement(self):
        plugin = get_k8s_plugin(_make_step(DeviceType.H100), "public.ecr.aws/img:latest")
        image = plugin["kubernetes"]["podSpec"]["containers"][0]["image"]
        assert "public.ecr.aws" not in image
        assert "936637512419.dkr.ecr.us-west-2.amazonaws.com/vllm-ci-pull-through-cache" in image


class TestA100Plugin:
    def test_has_node_selector(self):
        plugin = get_k8s_plugin(_make_step(DeviceType.A100), "img:latest")
        node_sel = plugin["kubernetes"]["podSpec"]["nodeSelector"]
        assert node_sel == {"nvidia.com/gpu.product": "NVIDIA-A100-SXM4-80GB"}

    def test_has_priority_class(self):
        plugin = get_k8s_plugin(_make_step(DeviceType.A100), "img:latest")
        assert plugin["kubernetes"]["podSpec"]["priorityClassName"] == "ci"


class TestH200Plugin:
    def test_has_node_selector(self):
        plugin = get_k8s_plugin(_make_step(DeviceType.H200), "img:latest")
        node_sel = plugin["kubernetes"]["podSpec"]["nodeSelector"]
        assert node_sel == {"node.kubernetes.io/instance-type": "gpu-h200-sxm"}

    def test_hardcoded_8_gpus(self):
        plugin = get_k8s_plugin(_make_step(DeviceType.H200, num_devices=2), "img:latest")
        limits = plugin["kubernetes"]["podSpec"]["containers"][0]["resources"]["limits"]
        assert limits["nvidia.com/gpu"] == 8


class TestUnsupportedDevice:
    def test_raises_value_error(self):
        with pytest.raises(ValueError, match="Unsupported K8s device type"):
            get_k8s_plugin(_make_step(DeviceType.CPU), "img:latest")


class TestCommonEnv:
    def test_contains_vllm_usage_source(self):
        plugin = get_k8s_plugin(_make_step(DeviceType.H100), "img:latest")
        env = plugin["kubernetes"]["podSpec"]["containers"][0]["env"]
        names = [e["name"] for e in env]
        assert "VLLM_USAGE_SOURCE" in names

    def test_contains_hf_token_secret_ref(self):
        plugin = get_k8s_plugin(_make_step(DeviceType.A100), "img:latest")
        env = plugin["kubernetes"]["podSpec"]["containers"][0]["env"]
        hf_token = [e for e in env if e["name"] == "HF_TOKEN"][0]
        assert hf_token["valueFrom"]["secretKeyRef"]["name"] == "hf-token-secret"


class TestNumDevices:
    def test_default_num_devices(self):
        plugin = get_k8s_plugin(_make_step(DeviceType.H100), "img:latest")
        limits = plugin["kubernetes"]["podSpec"]["containers"][0]["resources"]["limits"]
        assert limits["nvidia.com/gpu"] == 1

    def test_custom_num_devices(self):
        plugin = get_k8s_plugin(_make_step(DeviceType.A100, num_devices=4), "img:latest")
        limits = plugin["kubernetes"]["podSpec"]["containers"][0]["resources"]["limits"]
        assert limits["nvidia.com/gpu"] == 4


class TestIndependentDicts:
    def test_two_calls_return_independent_dicts(self):
        plugin1 = get_k8s_plugin(_make_step(DeviceType.H100), "img:v1")
        plugin2 = get_k8s_plugin(_make_step(DeviceType.H100), "img:v2")
        # Mutate plugin1 and verify plugin2 is unaffected.
        plugin1["kubernetes"]["podSpec"]["containers"][0]["env"].append(
            {"name": "EXTRA", "value": "val"}
        )
        env2_names = [
            e["name"]
            for e in plugin2["kubernetes"]["podSpec"]["containers"][0]["env"]
        ]
        assert "EXTRA" not in env2_names
