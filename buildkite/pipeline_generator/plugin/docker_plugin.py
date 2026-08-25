from step import Step
from constants import DOCKER_CHECKOUT_MOUNT_PATH, DeviceType
from fnrec_payload import fnrec_enabled
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
        "RAY_COMPAT_SLACK_WEBHOOK_URL",
    ],
    "volumes": [
        "/dev/shm:/dev/shm",
        "/fsx/hf_cache:/fsx/hf_cache",
    ],
}

h200_18gb_plugin_template = {
    "image": "",
    "always-pull": True,
    "propagate-environment": True,
    "environment": [
        "VLLM_USAGE_SOURCE=ci-test",
        "NCCL_CUMEM_HOST_ENABLE=0",
        "PYTORCH_CUDA_ALLOC_CONF=expandable_segments:False",
        "HF_TOKEN",
        "HF_HOME",
        "CODECOV_TOKEN",
        "BUILDKITE_ANALYTICS_TOKEN",
        "CUDA_VISIBLE_DEVICES",
        "NVIDIA_VISIBLE_DEVICES",
    ],
    "volumes": [
        "/dev/shm:/dev/shm",
        "/mnt/vllm-ci:/mnt/vllm-ci",
        "/dev/nvidiactl:/dev/nvidiactl",
    ],
}

h200_35gb_plugin_template = {
    "image": "",
    "always-pull": True,
    "propagate-environment": True,
    "environment": [
        "VLLM_USAGE_SOURCE=ci-test",
        "NCCL_CUMEM_HOST_ENABLE=0",
        "PYTORCH_CUDA_ALLOC_CONF=expandable_segments:False",
        "HF_TOKEN",
        "HF_HOME",
        "CODECOV_TOKEN",
        "BUILDKITE_ANALYTICS_TOKEN",
        "CUDA_VISIBLE_DEVICES",
        "NVIDIA_VISIBLE_DEVICES",
    ],
    "volumes": [
        "/dev/shm:/dev/shm",
        "/mnt/vllm-ci:/mnt/vllm-ci",
        "/dev/nvidiactl:/dev/nvidiactl",
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
        "HF_HOME",
        "HF_TOKEN",
        "CODECOV_TOKEN",
        "BUILDKITE_ANALYTICS_TOKEN",
    ],
    "volumes": [
        "/dev/shm:/dev/shm",
        "/raid:/raid",
        "/mnt/shared:/mnt/shared",
    ],
}

amd_zen5_plugin_template = {
    "image": "",
    "always-pull": True,
    "propagate-environment": True,
    "environment": [
        "VLLM_USAGE_SOURCE=ci-test",
        "NCCL_CUMEM_HOST_ENABLE=0",
        "HF_HOME",
        "HF_TOKEN",
        "CODECOV_TOKEN",
        "BUILDKITE_ANALYTICS_TOKEN",
    ],
    "volumes": [
        "/dev/shm:/dev/shm",
        "/mnt/ci-cache:/mnt/ci-cache",
    ],
}

def get_docker_plugin(step: Step, image: str):
    plugin = None
    if step.device == DeviceType.H200_18GB:
        plugin = copy.deepcopy(h200_18gb_plugin_template)
    elif step.device == DeviceType.H200_35GB:
        plugin = copy.deepcopy(h200_35gb_plugin_template)
    elif step.device == DeviceType.H200:
        plugin = copy.deepcopy(h200_plugin_template)
    elif step.device == DeviceType.B200:
        plugin = copy.deepcopy(b200_plugin_template)
    elif step.device == DeviceType.AMD_ZEN5_CPU:
        plugin = copy.deepcopy(amd_zen5_plugin_template)
    else:
        plugin = copy.deepcopy(docker_plugin_template)
    plugin["image"] = image

    if step.device in (DeviceType.H200_18GB, DeviceType.H200_35GB):
        image = image.replace("public.ecr.aws", "936637512419.dkr.ecr.us-west-2.amazonaws.com/vllm-ci-pull-through-cache")
        plugin["image"] = image
    # Stated, not inherited, but only where something depends on it. These are
    # already the plugin's defaults; fnrec writes its recording at this path and
    # delivery is the step's `artifact_paths`, so a plugin bump that moved the
    # mount would silently relocate every recording. Gated so the generated YAML
    # stays byte-identical while fnrec is off.
    if fnrec_enabled():
        plugin["mount-checkout"] = True
        plugin["workdir"] = DOCKER_CHECKOUT_MOUNT_PATH
    # OTel uploads from inside the container, so it needs the agent binary.
    # fnrec no longer does: the agent collects its output from the checkout after
    # the container exits. k8s needs no equivalent; agent-stack-k8s already
    # copies the binary into the pod.
    if (
        step.label == "Benchmarks"
        or step.mount_buildkite_agent
        or step.otel_tracing_enabled()
    ):
        plugin["mount_buildkite_agent"] = True
    if step.device in (DeviceType.CPU, DeviceType.CPU_SMALL, DeviceType.CPU_MEDIUM) and plugin.get("gpus"):
        del plugin["gpus"]
    return plugin
