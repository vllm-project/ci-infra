import copy
from step import Step
from constants import DeviceType
from plugin.analytics import get_buildkite_analytics_token_env

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
                        get_buildkite_analytics_token_env(),
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
                        get_buildkite_analytics_token_env(),
                    ],
                }
            ],
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
                        get_buildkite_analytics_token_env(),
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

b200_plugin_template = {
    "kubernetes": {
        "podSpec": {
            "runtimeClassName": "nvidia",
            "hostNetwork": True,
            "dnsPolicy": "ClusterFirstWithHostNet",
            "imagePullSecrets": [
                {"name": "k8s-ecr-login-renew-docker-secret"},
            ],
            "containers": [
                {
                    "image": "",
                    "resources": {"limits": {"nvidia.com/gpu": ""}},
                    "securityContext": {
                        "capabilities": {
                            "add": ["IPC_LOCK", "SYS_RESOURCE"],
                        },
                    },
                    "volumeMounts": [
                        {"name": "devshm", "mountPath": "/dev/shm"},
                        {"name": "raid", "mountPath": "/raid"},
                        {"name": "shared", "mountPath": "/mnt/shared"},
                    ],
                    "env": [
                        {"name": "VLLM_USAGE_SOURCE", "value": "ci-test"},
                        {"name": "NCCL_CUMEM_HOST_ENABLE", "value": "0"},
                        {"name": "HF_HOME", "value": "/raid/hf_cache"},
                        {
                            "name": "HF_TOKEN",
                            "valueFrom": {
                                "secretKeyRef": {
                                    "name": "hf-token-secret",
                                    "key": "token",
                                }
                            },
                        },
                        get_buildkite_analytics_token_env(),
                    ],
                }
            ],
            "volumes": [
                {"name": "devshm", "emptyDir": {"medium": "Memory"}},
                {
                    "name": "raid",
                    "hostPath": {"path": "/raid", "type": "DirectoryOrCreate"},
                },
                {
                    "name": "shared",
                    "hostPath": {"path": "/mnt/shared", "type": "DirectoryOrCreate"},
                },
            ],
        }
    }
}

h100_rh_plugin_template = {
    "kubernetes": {
        "podSpec": {
            "serviceAccountName": "buildkite-anyuid",
            "securityContext": {
                "fsGroup": 0
            },
            "containers": [
                {
                    "image": "",
                    "resources": {"limits": {"nvidia.com/gpu": ""}},
                    "securityContext": {
                        "runAsUser": 0,
                        "runAsGroup": 0
                    },
                    "volumeMounts": [
                        {"name": "devshm", "mountPath": "/dev/shm"},
                        {"name": "ci-cache", "mountPath": "/ci-cache"},
                    ],
                    "env": [
                        {"name": "VLLM_USAGE_SOURCE", "value": "ci-test"},
                        {"name": "NCCL_CUMEM_HOST_ENABLE", "value": "0"},
                        {"name": "HF_HOME", "value": "/ci-cache/hf_home"},
                        {
                            "name": "HF_TOKEN",
                            "valueFrom": {
                                "secretKeyRef": {
                                    "name": "hf-token-secret",
                                    "key": "token",
                                }
                            },
                        },
                        get_buildkite_analytics_token_env(),
                    ],
                }
            ],
            "nodeSelector": {"vllm.ci/gpu-pool": "upstream-ci-h100"},
            "volumes": [
                {"name": "devshm", "emptyDir": {"medium": "Memory"}},
                {
                    "name": "ci-cache",
                    "hostPath": {"path": "/var/mnt/ci-cache", "type": "DirectoryOrCreate"},
                },
            ],
        }
    }
}


# Per-GPU sizing constants (g6.12xlarge = 48 vCPU / 192 GiB / 4 GPUs).
L4_CPU_PER_GPU = 10          # requests only; no cpu limit (jobs are GPU-bound)
L4_MEM_PER_GPU = "40Gi"      # requests only; scheduling honesty, not a cap
L4_SHM_PER_GPU = "24Gi"      # 4-GPU job -> 96Gi = today's effective node RAM/2

l4_plugin_template = {
    "kubernetes": {
        "metadata": {
            "annotations": {
                # never let the autoscaler evict a running CI job
                "cluster-autoscaler.kubernetes.io/safe-to-evict": "false",
                "karpenter.sh/do-not-disrupt": "true",  # future-proof
            },
        },
        "podSpec": {
            "priorityClassName": "ci",
            "serviceAccountName": "buildkite-gpu-jobs",
            "tolerations": [{
                "key": "nvidia.com/gpu", "operator": "Exists",
                "effect": "NoSchedule",
            }],
            "nodeSelector": {"vllm.ci/gpu-pool": "l4x4"},
            "containers": [
                {
                    "image": "",
                    "resources": {
                        "limits": {"nvidia.com/gpu": ""},
                        "requests": {"nvidia.com/gpu": "", "cpu": "", "memory": ""},
                    },
                    "volumeMounts": [
                        {"name": "devshm", "mountPath": "/dev/shm"},
                        {"name": "hf-cache", "mountPath": "/fsx/hf_cache"},
                    ],
                    "env": [
                        {"name": "VLLM_USAGE_SOURCE", "value": "ci-test"},
                        {"name": "NCCL_CUMEM_HOST_ENABLE", "value": "0"},
                        {"name": "HF_HOME", "value": "/fsx/hf_cache"},
                        {"name": "HF_HUB_ENABLE_HF_TRANSFER", "value": "1"},
                        {"name": "HF_TOKEN", "valueFrom": {"secretKeyRef": {
                            "name": "hf-token-secret", "key": "token"}}},
                        {"name": "BUILDKITE_ANALYTICS_TOKEN", "valueFrom": {"secretKeyRef": {
                            "name": "buildkite-analytics-token", "key": "token",
                            "optional": True}}},
                    ],
                }
            ],
            "volumes": [
                {"name": "devshm",
                 "emptyDir": {"medium": "Memory", "sizeLimit": ""}},
                {"name": "hf-cache",
                 "hostPath": {"path": "/fsx/hf_cache", "type": "DirectoryOrCreate"}},
            ],
        },
    }
}


def get_k8s_plugin(step: Step, image: str):
    plugin = None
    if step.device == DeviceType.H100:
        # Use RedHat template for multi-GPU tests (4+ GPUs)
        if step.num_devices is not None and step.num_devices >= 4:
            plugin = copy.deepcopy(h100_rh_plugin_template)
        else:
            plugin = copy.deepcopy(h100_plugin_template)
    elif step.device == DeviceType.H200:
        plugin = copy.deepcopy(nebius_h200_plugin_template)
    elif step.device == DeviceType.A100.value:
        plugin = copy.deepcopy(a100_plugin_template)
    elif step.device == DeviceType.B200_K8S:
        plugin = copy.deepcopy(b200_plugin_template)
    elif step.device == DeviceType.L4:
        plugin = copy.deepcopy(l4_plugin_template)

    if step.device in (DeviceType.H100, DeviceType.B200_K8S):
        image = image.replace("public.ecr.aws", "936637512419.dkr.ecr.us-west-2.amazonaws.com/vllm-ci-pull-through-cache")
    plugin["kubernetes"]["podSpec"]["containers"][0]["image"] = image
    plugin["kubernetes"]["podSpec"]["containers"][0]["resources"]["limits"][
        "nvidia.com/gpu"
    ] = step.num_devices or 1

    if step.device == DeviceType.L4:
        num_gpus = step.num_devices or 1
        pod_spec = plugin["kubernetes"]["podSpec"]
        requests = pod_spec["containers"][0]["resources"]["requests"]
        requests["nvidia.com/gpu"] = num_gpus
        requests["cpu"] = str(num_gpus * L4_CPU_PER_GPU)
        requests["memory"] = f"{num_gpus * int(L4_MEM_PER_GPU[:-2])}Gi"
        pod_spec["volumes"][0]["emptyDir"]["sizeLimit"] = (
            f"{num_gpus * int(L4_SHM_PER_GPU[:-2])}Gi"
        )
    return plugin
