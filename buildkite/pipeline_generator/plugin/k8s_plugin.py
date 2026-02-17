import copy
from step import Step
from constants import DeviceType

HF_HOME = "/root/.cache/huggingface"

nebius_h200_plugin_template = {
    "kubernetes": {
        "podSpec": {
            "containers": [
                {
                    "image": "",
                    "resources": {
                        "limits": {
                            "nvidia.com/gpu": 8
                        }
                    },
                    "volumeMounts": [
                        {"name": "devshm", "mountPath": "/dev/shm"},
                        {"name": "hf-cache", "mountPath": "/root/.cache/huggingface"},
                    ],
                    "env": [
                        {"name": "VLLM_USAGE_SOURCE", "value": "ci-test"},
                        {"name": "NCCL_CUMEM_HOST_ENABLE", "value": "0"},
                        {"name": "HF_HOME", "value": "/root/.cache/huggingface"},
                        {
                            "name": "HF_TOKEN",
                            "valueFrom": {
                                "secretKeyRef": {
                                    "name": "hf-token-secret",
                                    "key": "token",
                                }
                            },
                        },
                    ],
                }
            ],
            "nodeSelector": {"node.kubernetes.io/instance-type": "gpu-h200-sxm"},
            "volumes": [
                {"name": "devshm", "emptyDir": {"medium": "Memory"}},
                {
                    "name": "hf-cache",
                    "hostPath": {"path": "/mnt/hf-cache", "type": "DirectoryOrCreate"},
                },
            ],
        }
    }
}

h100_plugin_template = {
    "kubernetes": {
        "podSpec": {
            "containers": [
                {
                    "image": "",
                    "resources": {"limits": {"nvidia.com/gpu": ""}},
                    "volumeMounts": [
                        {"name": "devshm", "mountPath": "/dev/shm"},
                        {"name": "hf-cache", "mountPath": HF_HOME},
                    ],
                    "env": [
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
                    ],
                }
            ],
            "nodeSelector": {"node.kubernetes.io/instance-type": "gpu-h100-sxm"},
            "volumes": [
                {"name": "devshm", "emptyDir": {"medium": "Memory"}},
                {
                    "name": "hf-cache",
                    "hostPath": {"path": "/mnt/hf-cache", "type": "DirectoryOrCreate"},
                },
            ],
        }
    }
}

a100_plugin_template = {
    "kubernetes": {
        "podSpec": {
            "priorityClassName": "ci",
            "containers": [
                {
                    "image": "",
                    "resources": {"limits": {"nvidia.com/gpu": ""}},
                    "volumeMounts": [
                        {"name": "devshm", "mountPath": "/dev/shm"},
                        {"name": "hf-cache", "mountPath": HF_HOME},
                    ],
                    "env": [
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
                    ],
                }
            ],
            "nodeSelector": {"nvidia.com/gpu.product": "NVIDIA-A100-SXM4-80GB"},
            "volumes": [
                {"name": "devshm", "emptyDir": {"medium": "Memory"}},
                {
                    "name": "hf-cache",
                    "hostPath": {"path": "/mnt/hf-cache", "type": "DirectoryOrCreate"},
                },
            ],
        }
    }
}


def get_k8s_plugin(step: Step, image: str):
    plugin = None
    if step.device == DeviceType.H100:
        plugin = copy.deepcopy(h100_plugin_template)
    elif step.device == DeviceType.H200:
        plugin = copy.deepcopy(nebius_h200_plugin_template)
    elif step.device == DeviceType.A100.value:
        plugin = copy.deepcopy(a100_plugin_template)

    if step.device == DeviceType.H100:
        image = image.replace("public.ecr.aws", "936637512419.dkr.ecr.us-west-2.amazonaws.com/vllm-ci-pull-through-cache")
    plugin["kubernetes"]["podSpec"]["containers"][0]["image"] = image
    plugin["kubernetes"]["podSpec"]["containers"][0]["resources"]["limits"][
        "nvidia.com/gpu"
    ] = step.num_devices or 1
    return plugin
