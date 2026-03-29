import copy
from step import Step
from constants import DeviceType

HF_HOME = "/root/.cache/huggingface"

_COMMON_ENV = [
    {"name": "VLLM_USAGE_SOURCE", "value": "ci-test"},
    {"name": "NCCL_CUMEM_HOST_ENABLE", "value": "0"},
    {"name": "HF_HOME", "value": HF_HOME},
    {
        "name": "HF_TOKEN",
        "valueFrom": {
            "secretKeyRef": {
                "name": "hf-token-secret",
                "key": "token",
            }
        },
    },
]

_COMMON_VOLUME_MOUNTS = [
    {"name": "devshm", "mountPath": "/dev/shm"},
    {"name": "hf-cache", "mountPath": HF_HOME},
]

_COMMON_VOLUMES = [
    {"name": "devshm", "emptyDir": {"medium": "Memory"}},
    {
        "name": "hf-cache",
        "hostPath": {"path": "/mnt/hf-cache", "type": "DirectoryOrCreate"},
    },
]


def _build_k8s_template(num_devices, node_selector=None, priority_class=None):
    """Build a K8s plugin template with device-specific overrides."""
    pod_spec = {
        "containers": [
            {
                "image": "",
                "resources": {"limits": {"nvidia.com/gpu": num_devices}},
                "volumeMounts": copy.deepcopy(_COMMON_VOLUME_MOUNTS),
                "env": copy.deepcopy(_COMMON_ENV),
            }
        ],
        "volumes": copy.deepcopy(_COMMON_VOLUMES),
    }
    if node_selector:
        pod_spec["nodeSelector"] = node_selector
    if priority_class:
        pod_spec["priorityClassName"] = priority_class
    return {"kubernetes": {"podSpec": pod_spec}}


def get_k8s_plugin(step: Step, image: str):
    if step.device == DeviceType.H100:
        plugin = _build_k8s_template(step.num_devices or 1)
    elif step.device == DeviceType.H200:
        plugin = _build_k8s_template(
            num_devices=8,
            node_selector={"node.kubernetes.io/instance-type": "gpu-h200-sxm"},
        )
    elif step.device == DeviceType.A100:
        plugin = _build_k8s_template(
            num_devices=step.num_devices or 1,
            node_selector={"nvidia.com/gpu.product": "NVIDIA-A100-SXM4-80GB"},
            priority_class="ci",
        )
    else:
        raise ValueError(f"Unsupported K8s device type: {step.device}")

    if step.device == DeviceType.H100:
        image = image.replace("public.ecr.aws", "936637512419.dkr.ecr.us-west-2.amazonaws.com/vllm-ci-pull-through-cache")
    plugin["kubernetes"]["podSpec"]["containers"][0]["image"] = image
    return plugin
