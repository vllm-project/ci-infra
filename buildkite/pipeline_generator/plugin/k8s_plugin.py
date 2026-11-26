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


# Per-GPU sizing for intel-gpu-k8s.
INTEL_CPU_REQUEST_PER_GPU = 8
INTEL_CPU_LIMIT_PER_GPU = 32
INTEL_MEM_REQUEST_PER_GPU = 32
INTEL_MEM_LIMIT_PER_GPU = 64
INTEL_SHM_PER_GPU = 8

intel_gpu_k8s_plugin_template = {
    "kubernetes": {
        "checkout": {"skip": True},
        "podSpec": {
            "resourceClaims": [
                {"name": "gpu", "resourceClaimTemplateName": ""},
            ],
            "containers": [
                {
                    "image": "",
                    "env": [
                        {"name": "VLLM_USAGE_SOURCE", "value": "ci-test"},
                        {"name": "VLLM_TEST_DEVICE", "value": "xpu"},
                        {"name": "VLLM_DISABLE_COMPILE_CACHE", "value": "1"},
                        {"name": "HF_HOME", "value": "/data/huggingface"},
                        {
                            "name": "HF_TOKEN",
                            "valueFrom": {
                                "secretKeyRef": {
                                    "name": "hf-token-secret",
                                    "key": "token",
                                },
                            },
                        },
                        get_buildkite_analytics_token_env(),
                    ],
                    "resources": {
                        "claims": [{"name": "gpu"}],
                        "requests": {"cpu": "", "memory": ""},
                        "limits": {"cpu": "", "memory": ""},
                    },
                    "volumeMounts": [
                        {"name": "dshm", "mountPath": "/dev/shm"},
                        {
                            "name": "hf-cache",
                            "mountPath": "/data/huggingface",
                        },
                    ],
                },
            ],
            "volumes": [
                {
                    "name": "dshm",
                    "emptyDir": {"medium": "Memory", "sizeLimit": ""},
                },
                {
                    "name": "hf-cache",
                    "hostPath": {
                        "path": "/data/huggingface",
                        "type": "DirectoryOrCreate",
                    },
                },
            ],
        },
    },
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
    "kernels-b200": 90000,
    "deepseek-v4-kernel-test-b200": 8192,
    "kernels-minimax-m3-attention-b200": 8192,
    "fusion-and-compile-unit-tests-2xb200": 8192,
    "kernels-fusedmoe-layer-test-2-b200s": 24576,
    "fusion-e2e-tp2-b200": 90000,
    "kernels-fla-ops-test-b200": 32768,
    "qwen4-exp-kernels-b200": 40960,
    "kernels-gate-linear-cutedsl-b200": 8192,
    "minimax-m3-model-tests-b200": 49152,
    "mistral-large-4-unit-tests-b200": 8192,
    "minimax-m3-mega-moe-test-b200": 8192,
    "shardq-metadata-b200": 4096,
    "fusion-e2e-config-sweep-b200": 90000,
    "kernels-fp4-moe-test-b200": 73728,
    "weight-reload-refit-nightly": 90000,
    "spec-decode-draft-model-nightly-b200": 90000,
    "spec-decode-speculators-mtp-nightly-b200": 90000,
    "spec-decode-eagle-nightly-b200": 90000,
    "spec-decode-dflash2-nightly-b200": 90000,
    "preload-mooncake-store-2xb200": 90000,
    "qwen3-5-unit-tests-b200": 16384,
    "gemm-rs-ar-2xb200": 12288,
    "multi-modal-flashinfer-autotuning": 122880,
    "dsv41-nvidia-kernels-4xb200": 16384,
    "glm5next-nvidia-specialized-decode-4xb200": 24576,
}

# The same for H100 (80 GB cards). A quota is measured on one device type, so
# each device has its own table: a step that moves keeps whole cards.
HAMI_GPUMEM_MIB_H100 = {
    "v1-attention-h100-mi300": 32768,
    "deepseek-v4-kernel-test-h100": 16384,
    "kernels-deepgemm-test-h100": 20480,
    "kernels-minimax-m3-sparse-prefill-h100": 8192,
    "kernels-bf16x3-router-gemm-h100": 8192,
    "kernels-attention-diffkv-test-h100": 8192,
    "kernels-fusedmoe-layer-test-2-h100s": 20480,
    "distributed-compile-unit-tests-2xh100": 12288,
    "kernels-minimax-reduce-rms-test-2-gpus": 12288,
    "kernels-fp8-moe-test-2xh100": 16384,
    "fusion-e2e-tp2-quick-h100": 40000,
    "fault-tolerance-e2e-4xh100": 40000,
    "dsv41-nvidia-sm80-sm90-decode-8xh100": 12288,
}

# Steps whose quota was staged on more than one GPU, with that GPU count. Every
# other listed step was staged on one. A step whose GPU count changes keeps
# whole cards until it is staged again.
HAMI_GPUS = {
    "kernels-fusedmoe-layer-test-2-b200s": 2,
    "fusion-e2e-tp2-b200": 2,
    "kernels-fusedmoe-layer-test-2-h100s": 2,
    "distributed-compile-unit-tests-2xh100": 2,
    "kernels-minimax-reduce-rms-test-2-gpus": 2,
    "kernels-fp8-moe-test-2xh100": 2,
    "fusion-e2e-tp2-quick-h100": 2,
    "fault-tolerance-e2e-4xh100": 4,
    "dsv41-nvidia-sm80-sm90-decode-8xh100": 8,
    "preload-mooncake-store-2xb200": 2,
    "qwen3-5-unit-tests-b200": 2,
    "gemm-rs-ar-2xb200": 2,
    "multi-modal-flashinfer-autotuning": 2,
    "dsv41-nvidia-kernels-4xb200": 4,
    "glm5next-nvidia-specialized-decode-4xb200": 4,
}

# Per-GPU CPU sizing for H100 HAMi pods (160 cores / 8 GPUs = 20 cores/GPU).
# Prevents a single CPU-heavy test pod from starving co-located pods on the
# shared node while keeping total requests low enough not to block GPU packing.
H100_HAMI_CPU_REQUEST_PER_GPU = 4
H100_HAMI_CPU_LIMIT_PER_GPU = 20
H100_HAMI_MAX_CPU_LIMIT = 80


def get_k8s_plugin(step: Step, image: str):
    if step.device == DeviceType.INTEL_GPU_K8S:
        num_gpus = step.num_devices or 1
        plugin = copy.deepcopy(intel_gpu_k8s_plugin_template)
        pod_spec = plugin["kubernetes"]["podSpec"]
        pod_spec["resourceClaims"][0]["resourceClaimTemplateName"] = f"b70-{num_gpus}"
        pod_spec["containers"][0]["image"] = image
        resources = pod_spec["containers"][0]["resources"]
        resources["requests"] = {
            "cpu": str(INTEL_CPU_REQUEST_PER_GPU * num_gpus),
            "memory": f"{INTEL_MEM_REQUEST_PER_GPU * num_gpus}Gi",
        }
        resources["limits"] = {
            "cpu": str(INTEL_CPU_LIMIT_PER_GPU * num_gpus),
            "memory": f"{INTEL_MEM_LIMIT_PER_GPU * num_gpus}Gi",
        }
        pod_spec["volumes"][0]["emptyDir"]["sizeLimit"] = (
            f"{INTEL_SHM_PER_GPU * num_gpus}Gi"
        )
        return plugin

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

    quotas = {
        DeviceType.B200_K8S: HAMI_GPUMEM_MIB,
        DeviceType.H100: HAMI_GPUMEM_MIB_H100,
    }.get(step.device, {})
    if step.key in quotas and (step.num_devices or 1) == HAMI_GPUS.get(step.key, 1):
        # HAMi's webhook only handles Pods carrying this label.
        metadata = plugin["kubernetes"].setdefault("metadata", {})
        metadata.setdefault("labels", {})["vllm.ci/gpu-sharing"] = "hami"
        # With several sharing nodes, use the least-loaded node, then pack its cards.
        annotations = metadata.setdefault("annotations", {})
        annotations["hami.io/node-scheduler-policy"] = "spread"
        annotations["hami.io/gpu-scheduler-policy"] = "binpack"
        pod_spec = plugin["kubernetes"]["podSpec"]
        pod_spec.setdefault("nodeSelector", {})["gpu"] = "on"
        # The GPU Operator and HAMi daemonsets already tolerate this taint key.
        pod_spec.setdefault("tolerations", []).append(
            {
                "key": "nvidia.com/gpu",
                "operator": "Equal",
                "value": "hami",
                "effect": "NoSchedule",
            }
        )
        quota_mib = quotas[step.key]
        container = pod_spec["containers"][0]
        container["resources"]["limits"]["nvidia.com/gpumem"] = quota_mib
        container.setdefault("env", []).append(
            {"name": "VLLM_CI_HAMI_GPUMEM_MIB", "value": str(quota_mib)}
        )
        if step.device == DeviceType.H100:
            num_gpus = step.num_devices or 1
            container["resources"].setdefault("requests", {})["cpu"] = str(
                num_gpus * H100_HAMI_CPU_REQUEST_PER_GPU
            )
            container["resources"]["limits"]["cpu"] = str(
                min(num_gpus * H100_HAMI_CPU_LIMIT_PER_GPU, H100_HAMI_MAX_CPU_LIMIT)
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
