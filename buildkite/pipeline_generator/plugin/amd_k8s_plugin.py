from typing import Any, Dict, Mapping


def get_amd_k8s_plugin(
    *,
    image: str,
    gpu_count: int,
    workspace: str,
    workspace_volume_name: str,
    shm_size: str,
    container_env: Mapping[str, str],
) -> Dict[str, Any]:
    """Build the Kubernetes pod patch for native AMD test execution."""
    # The Buildkite controller decodes podSpecPatch into a typed PodSpec before
    # merging it. An explicit zero therefore overrides the controller's GPU
    # default; omitting the key would preserve the inherited request.
    gpu_resource = str(gpu_count)
    return {
        "kubernetes": {
            "podSpecPatch": {
                "automountServiceAccountToken": False,
                "securityContext": {"seccompProfile": {"type": "RuntimeDefault"}},
                "imagePullSecrets": [{"name": "docker-config"}],
                "containers": [
                    {
                        "name": "container-0",
                        "image": image,
                        "imagePullPolicy": "Always",
                        "securityContext": {
                            "allowPrivilegeEscalation": False,
                            "capabilities": {"add": ["IPC_LOCK"]},
                        },
                        "resources": {
                            "limits": {"amd.com/gpu": gpu_resource},
                            "requests": {"amd.com/gpu": gpu_resource},
                        },
                        "volumeMounts": [
                            {"name": "devshm", "mountPath": "/dev/shm"},
                            {
                                "name": workspace_volume_name,
                                "mountPath": workspace,
                            },
                        ],
                        "env": [
                            {"name": name, "value": value}
                            for name, value in container_env.items()
                        ],
                    }
                ],
                "volumes": [
                    {
                        "name": "devshm",
                        "emptyDir": {
                            "medium": "Memory",
                            "sizeLimit": shm_size,
                        },
                    },
                    {"name": workspace_volume_name, "emptyDir": {}},
                ],
            }
        }
    }
