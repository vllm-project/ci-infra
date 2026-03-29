from step import Step
from constants import DeviceType

_COMMON_FIELDS = {
    "always-pull": True,
    "propagate-environment": True,
}

_COMMON_ENV = [
    "VLLM_USAGE_SOURCE=ci-test",
    "NCCL_CUMEM_HOST_ENABLE=0",
    "HF_TOKEN",
    "CODECOV_TOKEN",
    "BUILDKITE_ANALYTICS_TOKEN",
]

_DEVICE_CONFIGS = {
    DeviceType.H200: {
        "environment": _COMMON_ENV + ["HF_HOME"],
        "volumes": ["/dev/shm:/dev/shm", "/mnt/vllm-ci:/mnt/vllm-ci"],
        "gpus": "all",
    },
    DeviceType.B200: {
        "environment": _COMMON_ENV + ["HF_HOME"],
        "volumes": ["/dev/shm:/dev/shm", "/raid:/raid", "/mnt/shared:/mnt/shared"],
    },
}

_DEFAULT_CONFIG = {
    "environment": _COMMON_ENV + [
        "HF_HOME=/fsx/hf_cache",
        "RAY_COMPAT_SLACK_WEBHOOK_URL",
    ],
    "volumes": ["/dev/shm:/dev/shm", "/fsx/hf_cache:/fsx/hf_cache"],
    "gpus": "all",
}


def get_docker_plugin(step: Step, image: str):
    config = _DEVICE_CONFIGS.get(step.device, _DEFAULT_CONFIG)
    plugin = {
        "image": image,
        **_COMMON_FIELDS,
        "environment": list(config["environment"]),
        "volumes": list(config["volumes"]),
    }
    if "gpus" in config:
        plugin["gpus"] = config["gpus"]

    if step.label == "Benchmarks" or step.mount_buildkite_agent:
        plugin["mount_buildkite_agent"] = True
    if step.device in (DeviceType.CPU, DeviceType.CPU_SMALL, DeviceType.CPU_MEDIUM) and "gpus" in plugin:
        del plugin["gpus"]
    return plugin
