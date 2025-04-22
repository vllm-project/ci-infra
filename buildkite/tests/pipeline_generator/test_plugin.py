import pytest
import sys

from buildkite.pipeline_generator.plugin import (
    get_kubernetes_plugin_config,
    get_docker_plugin_config,
    DOCKER_PLUGIN_NAME,
    KUBERNETES_PLUGIN_NAME,
)


def test_get_kubernetes_plugin_config():
    docker_image_path = "test_image:latest"
    num_gpus = 1

    expected_config = {
        KUBERNETES_PLUGIN_NAME: {
            "podSpec": {
                "containers": [
                    {
                        "image": docker_image_path,
                        "resources": {"limits": {"nvidia.com/gpu": num_gpus}},
                        "volumeMounts": [
                            {"name": "devshm", "mountPath": "/dev/shm"},
                            {"name": "hf-cache", "mountPath": "/root/.cache/huggingface"}
                        ],
                        "env": [
                            {"name": "HF_HOME", "value": "/root/.cache/huggingface"},
                            {"name": "VLLM_USAGE_SOURCE", "value": "ci-test"},
                            {
                                "name": "HF_TOKEN",
                                "valueFrom": {
                                    "secretKeyRef": {
                                        "name": "hf-token-secret",
                                        "key": "token"
                                    }
                                }
                            },
                        ],
                    }
                ],
                "priorityClassName": "ci",
                "nodeSelector": {"nvidia.com/gpu.product": "NVIDIA-A100-SXM4-80GB"},
                "volumes": [
                    {"name": "devshm", "emptyDir": {"medium": "Memory"}},
                    {"name": "hf-cache", "hostPath": {"path": "/root/.cache/huggingface", "type": "Directory"}}
                ]
            }
        }
    }

    assert get_kubernetes_plugin_config(docker_image_path, num_gpus) == expected_config


@pytest.mark.parametrize(
    "docker_image_path, no_gpu, expected_config",
    [
        (
            "test_image:latest",
            False,
            {
                DOCKER_PLUGIN_NAME: {
                    "image": "test_image:latest",
                    "always-pull": True,
                    "propagate-environment": True,
                    "shell": ["/bin/bash", "-c"],
                    "gpus": "all",
                    "environment": [
                        "HF_HOME=/root/.cache/huggingface",
                        "VLLM_USAGE_SOURCE=ci-test",
                        "HF_TOKEN",
                        "BUILDKITE_ANALYTICS_TOKEN"
                    ],
                    "mount-buildkite-agent": False,
                    "volumes": [
                        "/dev/shm:/dev/shm",
                        "/root/.cache/huggingface:/root/.cache/huggingface"
                    ]
                }
            }
        ),
        (
            "cpu_image:latest",
            True,
            {
                DOCKER_PLUGIN_NAME: {
                    "image": "cpu_image:latest",
                    "always-pull": True,
                    "propagate-environment": True,
                    "shell": ["/bin/bash", "-c"],
                    "environment": [
                        "HF_HOME=/root/.cache/huggingface",
                        "VLLM_USAGE_SOURCE=ci-test",
                        "HF_TOKEN",
                        "BUILDKITE_ANALYTICS_TOKEN"
                    ],
                    "mount-buildkite-agent": False,
                    "volumes": [
                        "/dev/shm:/dev/shm",
                        "/root/.cache/huggingface:/root/.cache/huggingface"
                    ]
                }
            }
        ),
    ]
)
def test_get_docker_plugin_config(docker_image_path, no_gpu, expected_config):
    assert get_docker_plugin_config(docker_image_path, no_gpu) == expected_config


if __name__ == "__main__":
    sys.exit(pytest.main(["-v", __file__]))
