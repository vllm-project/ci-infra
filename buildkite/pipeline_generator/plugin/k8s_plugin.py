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
                    "resources": {"limits": {"nvidia.com/gpu": 8}},
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
            "securityContext": {"fsGroup": 0},
            "containers": [
                {
                    "image": "",
                    "resources": {"limits": {"nvidia.com/gpu": ""}},
                    "securityContext": {"runAsUser": 0, "runAsGroup": 0},
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
                    "hostPath": {
                        "path": "/var/mnt/ci-cache",
                        "type": "DirectoryOrCreate",
                    },
                },
            ],
        }
    }
}


# Per-GPU sizing constants (g6.12xlarge = 48 vCPU / 192 GiB / 4 GPUs).
L4_CPU_PER_GPU = 10  # requests only; no cpu limit (jobs are GPU-bound)
L4_MEM_PER_GPU = "40Gi"  # requests only; scheduling honesty, not a cap
L4_SHM_PER_GPU = "24Gi"  # 4-GPU job -> 96Gi = today's effective node RAM/2

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
            "tolerations": [
                {
                    "key": "nvidia.com/gpu",
                    "operator": "Exists",
                    "effect": "NoSchedule",
                }
            ],
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
                        {
                            "name": "HF_TOKEN",
                            "valueFrom": {
                                "secretKeyRef": {
                                    "name": "hf-token-secret",
                                    "key": "token",
                                }
                            },
                        },
                        {
                            "name": "BUILDKITE_ANALYTICS_TOKEN",
                            "valueFrom": {
                                "secretKeyRef": {
                                    "name": "buildkite-analytics-token",
                                    "key": "token",
                                    "optional": True,
                                }
                            },
                        },
                    ],
                }
            ],
            "volumes": [
                {"name": "devshm", "emptyDir": {"medium": "Memory", "sizeLimit": ""}},
                {
                    "name": "hf-cache",
                    "hostPath": {"path": "/fsx/hf_cache", "type": "DirectoryOrCreate"},
                },
            ],
        },
    }
}


# GPU-sharing pilot: B200 step key -> HAMi GPU memory quota in MiB. These steps
# run as a share of one card on the HAMi node (labelled gpu=on, tainted
# nvidia.com/gpu=hami) instead of taking a whole card. A vLLM engine wants 92%
# of the memory it sees free at start, so for steps that start engines the
# quota must be at least 12.5x what earlier tests in the same process still
# hold (v1-attention measured 1.7 GiB, kernels-root-misc 4.5 GiB).
# shortcut: a hand list for one node; move the quota into the step config if
# the pilot is extended.
HAMI_GPUMEM_MIB = {
    "v1-attention-b200": 32768,
    "kernels-root-misc-test-b200": 73728,
    "kimi-k3-unit-tests-b200": 6144,
    "kernels-mhc-test-b200": 4096,
    "kernels-b200": 92160,
    # wave 2 staging: generous first quotas, trimmed after measurement
    "deepseek-v4-kernel-test-b200": 16384,
    "kernels-minimax-m3-attention-b200": 16384,
    "inkling-unit-tests-b200": 40960,
    "kernels-fla-ops-test-b200": 40960,
    "qwen4-exp-kernels-b200": 49152,
    "kernels-gate-linear-cutedsl-b200": 12288,
    "minimax-m3-model-tests-b200": 49152,
    "mistral-large-4-unit-tests-b200": 12288,
    "minimax-m3-mega-moe-test-b200": 12288,
    "fusion-and-compile-unit-tests-2xb200": 16384,
    "shardq-metadata-b200": 8192,
    "weight-reload-refit": 92160,
    "quantized-moe-test-b200": 92160,
    "batch-invariance-b200": 92160,
    "lm-eval-humming-b200-act": 92160,
    "lm-eval-humming-b200-default": 92160,
    "lm-eval-small-models-1xb200": 92160,
    "spec-decode-draft-model-nightly-b200": 92160,
    "spec-decode-speculators-mtp-nightly-b200": 92160,
    "spec-decode-eagle-nightly-b200": 92160,
    "spec-decode-dflash2-nightly-b200": 92160,
    "fusion-e2e-config-sweep-b200": 92160,
    "weight-reload-refit-nightly": 92160,
    "kernels-fp4-moe-test-b200": 92160,
    "kernels-fusedmoe-layer-test-2-b200s": 32768,
    "preload-mooncake-store-2xb200": 92160,
    "qwen3-5-unit-tests-b200": 16384,
    "gemm-rs-ar-2xb200": 16384,
    "dsv41-nvidia-kernels-4xb200": 24576,
    "glm5next-nvidia-specialized-decode-4xb200": 32768,
    "fusion-e2e-tp2-b200": 92160,
    "weight-reload-refit-2gpu": 92160,
    "multi-modal-flashinfer-autotuning": 122880,
}


def get_k8s_plugin(step: Step, image: str):
    plugin = None
    if step.device == DeviceType.H100:
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
        image = image.replace(
            "public.ecr.aws",
            "936637512419.dkr.ecr.us-west-2.amazonaws.com/vllm-ci-pull-through-cache",
        )
    plugin["kubernetes"]["podSpec"]["containers"][0]["image"] = image
    plugin["kubernetes"]["podSpec"]["containers"][0]["resources"]["limits"][
        "nvidia.com/gpu"
    ] = step.num_devices or 1

    # Quotas were measured on one GPU; a step that grows keeps whole cards.
    if step.device == DeviceType.B200_K8S and step.key in HAMI_GPUMEM_MIB:
        # HAMi's webhook only handles Pods carrying this label.
        plugin["kubernetes"]["metadata"] = {"labels": {"vllm.ci/gpu-sharing": "hami"}}
        pod_spec = plugin["kubernetes"]["podSpec"]
        pod_spec["nodeSelector"] = {"gpu": "on"}
        # The GPU Operator and HAMi daemonsets already tolerate this taint key.
        pod_spec["tolerations"] = [
            {
                "key": "nvidia.com/gpu",
                "operator": "Equal",
                "value": "hami",
                "effect": "NoSchedule",
            }
        ]
        pod_spec["containers"][0]["resources"]["limits"]["nvidia.com/gpumem"] = (
            HAMI_GPUMEM_MIB[step.key]
        )

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
