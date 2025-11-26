from pydantic import BaseModel
from typing import List
from step import Step
from utils import GPUType

docker_plugin_template = {
    "image": "",
    "always_pull": True,
    "propagate_environment": True,
    "gpus": "all",
    "environment": [
        "VLLM_USAGE_SOURCE=ci-test",
        "NCCL_CUMEM_HOST_ENABLE=0",
        "HF_HOME=/fsx/hf_cache",
        "HF_TOKEN",
        "CODECOV_TOKEN",
    ],
    "volumes": [
        "/dev/shm:/dev/shm",
        "/fsx/hf_cache:/fsx/hf_cache",
    ],
}

h200_plugin_template = {
    "image": "",
    "always_pull": True,
    "propagate_environment": True,
    "gpus": "all",
    "environment": [
        "VLLM_USAGE_SOURCE=ci-test",
        "NCCL_CUMEM_HOST_ENABLE=0",
        "HF_HOME=/benchmark-hf-cache",
        "HF_TOKEN",
        "CODECOV_TOKEN",
    ],
    "volumes": [
        "/dev/shm:/dev/shm",
        "/data/benchmark-hf-cache:/benchmark-hf-cache",
        "/data/benchmark-vllm-cache:/root/.cache/vllm",
    ],
}

b200_plugin_template = {
    "image": "",
    "always_pull": True,
    "propagate_environment": True,
    "environment": [
        "VLLM_USAGE_SOURCE=ci-test",
        "NCCL_CUMEM_HOST_ENABLE=0",
        "HF_HOME=/benchmark-hf-cache",
        "HF_TOKEN",
        "CODECOV_TOKEN",
    ],
    "volumes": [
        "/dev/shm:/dev/shm",
        "/data/benchmark-hf-cache:/benchmark-hf-cache",
        "/data/benchmark-vllm-cache:/root/.cache/vllm",
    ],
}

def get_docker_plugin(step: Step, image: str):
    plugin = None
    if step.gpu == GPUType.H200:
        plugin = h200_plugin_template
    elif step.gpu == GPUType.B200:
        plugin = b200_plugin_template
    else:
        plugin = docker_plugin_template
    plugin["image"] = image

    if step.label == "Benchmarks" or step.mount_buildkite_agent:
        plugin["mount_buildkite_agent"] = True
        
    # TODO: Add BUILDKITE_ANALYTICS_TOKEN and pytest addopts for fail_fast 
    return plugin
