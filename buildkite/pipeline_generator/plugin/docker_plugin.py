from step import Step
from constants import DeviceType
import copy

docker_plugin_template = {
    "image": "",
    "always-pull": True,
    "propagate-environment": True,
    "gpus": "all",
    "environment": [
        "VLLM_USAGE_SOURCE=ci-test",
        "NCCL_CUMEM_HOST_ENABLE=0",
        "HF_HOME=/fsx/hf_cache",
        "HF_TOKEN",
        "CODECOV_TOKEN",
        "BUILDKITE_ANALYTICS_TOKEN",
    ],
    "volumes": [
        "/dev/shm:/dev/shm",
        "/fsx/hf_cache:/fsx/hf_cache",
    ],
}

h200_plugin_template = {
    "image": "",
    "always-pull": True,
    "propagate-environment": True,
    "gpus": "all",
    "environment": [
        "VLLM_USAGE_SOURCE=ci-test",
        "NCCL_CUMEM_HOST_ENABLE=0",
        "HF_TOKEN",
        "HF_HOME",
        "CODECOV_TOKEN",
        "BUILDKITE_ANALYTICS_TOKEN",
    ],
    "volumes": [
        "/dev/shm:/dev/shm",
        "/mnt/vllm-ci:/mnt/vllm-ci",
    ],
}

b200_plugin_template = {
    "image": "",
    "always-pull": True,
    "propagate-environment": True,
    "environment": [
        "VLLM_USAGE_SOURCE=ci-test",
        "NCCL_CUMEM_HOST_ENABLE=0",
        "HF_HOME=/benchmark-hf-cache",
        "HF_TOKEN",
        "CODECOV_TOKEN",
        "BUILDKITE_ANALYTICS_TOKEN",
    ],
    "volumes": [
        "/dev/shm:/dev/shm",
        "/data/benchmark-hf-cache:/benchmark-hf-cache",
        "/data/benchmark-vllm-cache:/root/.cache/vllm",
    ],
}


def get_docker_plugin(step: Step, image: str):
    plugin = None
    if step.device == DeviceType.H200:
        plugin = copy.deepcopy(h200_plugin_template)
    elif step.device == DeviceType.B200:
        plugin = copy.deepcopy(b200_plugin_template)
    else:
        plugin = copy.deepcopy(docker_plugin_template)
    plugin["image"] = image

    if step.label == "Benchmarks" or step.mount_buildkite_agent:
        plugin["mount_buildkite_agent"] = True
    if step.device == DeviceType.CPU and plugin.get("gpus"):
        del plugin["gpus"]
    # TODO: Add BUILDKITE_ANALYTICS_TOKEN and pytest addopts for fail_fast
    return plugin
