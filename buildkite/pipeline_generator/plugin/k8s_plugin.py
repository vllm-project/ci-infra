from step import Step
from utils import GPUType

HF_HOME = "/root/.cache/huggingface"

h100_plugin_template = {
    "kubernetes": {
        "podSpec": {
            "containers": [
                {
                    "image": "",
                    "command": [],
                    "resources": {
                        "limits": {
                            "nvidia.com/gpu": ""
                        }
                    },
                    "volumeMounts": [
                        {
                            "name": "devshm",
                            "mountPath": "/dev/shm"
                        },
                        {
                            "name": "hf-cache",
                            "mountPath": HF_HOME
                        }
                    ],
                    "env": [
                        {
                            "name": "VLLM_USAGE_SOURCE",
                            "value": "ci-test"
                        },
                        {
                            "name": "NCCL_CUMEM_HOST_ENABLE",
                            "value": "0"
                        },
                        {
                            "name": "HF_HOME",
                            "value": HF_HOME
                        },
                        {
                            "name": "HF_TOKEN",
                            "valueFrom": {
                                "secretKeyRef": {
                                    "name": "hf-token-secret",
                                    "key": "token"
                                }
                            }
                        }
                    ]
                }
            ],
            "nodeSelector": {
                "node.kubernetes.io/instance-type": "gpu-h100-sxm"
            },
            "volumes": [
                {
                    "name": "devshm",
                    "emptyDir": {
                        "medium": "Memory"
                    }
                },
                {
                    "name": "hf-cache",
                    "hostPath": {
                        "path": "/mnt/hf-cache",
                        "type": "DirectoryOrCreate"
                    }
                }
            ]
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
                    "command": [],
                    "resources": {
                        "limits": {
                            "nvidia.com/gpu": ""
                        }
                    },
                    "volumeMounts": [
                        {
                            "name": "devshm",
                            "mountPath": "/dev/shm"
                        },
                        {
                            "name": "hf-cache",
                            "mountPath": HF_HOME
                        }
                    ],
                    "env": [
                        {
                            "name": "VLLM_USAGE_SOURCE",
                            "value": "ci-test"
                        },
                        {
                            "name": "NCCL_CUMEM_HOST_ENABLE",
                            "value": "0"
                        },
                        {
                            "name": "HF_HOME",
                            "value": HF_HOME
                        },
                        {
                            "name": "HF_TOKEN",
                            "valueFrom": {
                                "secretKeyRef": {
                                    "name": "hf-token-secret",
                                    "key": "token"
                                }
                            }
                        }
                    ]
                }
            ],
            "nodeSelector": {
                "nvidia.com/gpu.product": "NVIDIA-A100-SXM4-80GB"
            },
            "volumes": [
                {
                    "name": "devshm",
                    "emptyDir": {
                        "medium": "Memory"
                    }
                },
                {
                    "name": "hf-cache",
                    "hostPath": {
                        "path": "/mnt/hf-cache",
                        "type": "DirectoryOrCreate"
                    }
                }
            ]
        }
    }
}

def get_k8s_plugin(step: Step, image: str):
    plugin = None
    print(step.gpu, step.gpu == GPUType.H100.value)
    if step.gpu == GPUType.H100.value:
        plugin = h100_plugin_template
    elif step.gpu == GPUType.A100.value:
        plugin = a100_plugin_template
    plugin["kubernetes"]["podSpec"]["containers"][0]["image"] = image
    plugin["kubernetes"]["podSpec"]["containers"][0]["resources"]["limits"]["nvidia.com/gpu"] = step.num_gpus or 1
    plugin["kubernetes"]["podSpec"]["containers"][0]["command"] = ["bash", "-c", f"(command nvidia-smi || true) && export VLLM_ALLOW_DEPRECATED_BEAM_SEARCH=1 && cd {step.working_dir} && {' && '.join(step.commands)}"]
    return plugin