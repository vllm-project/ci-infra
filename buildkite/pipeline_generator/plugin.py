from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from .utils.constants import (
    HF_HOME,
    EnvironmentValues,
    EnvironmentVariables,
    KubernetesConstants,
    PluginNames,
)

DOCKER_PLUGIN_NAME = PluginNames.DOCKER
KUBERNETES_PLUGIN_NAME = PluginNames.KUBERNETES

DEFAULT_DOCKER_ENVIRONMENT_VARIBLES = [
    f"HF_HOME={HF_HOME}",
    f"{EnvironmentVariables.VLLM_USAGE_SOURCE}={EnvironmentValues.VLLM_USAGE_CI_TEST}",
    "HF_TOKEN",
    "BUILDKITE_ANALYTICS_TOKEN",
]
DEFAULT_DOCKER_VOLUMES = ["/dev/shm:/dev/shm", f"{HF_HOME}:{HF_HOME}"]
DEFAULT_KUBERNETES_CONTAINER_VOLUME_MOUNTS: List[Dict[str, str]] = [
    {"name": KubernetesConstants.DEVSHM_VOLUME, "mountPath": KubernetesConstants.DEV_SHM_PATH},
    {"name": KubernetesConstants.HF_CACHE_VOLUME, "mountPath": HF_HOME},
]
DEFAULT_KUBERNETES_CONTAINER_ENVIRONMENT_VARIABLES: List[Dict[str, Any]] = [
    {"name": "HF_HOME", "value": HF_HOME},
    {"name": EnvironmentVariables.VLLM_USAGE_SOURCE, "value": EnvironmentValues.VLLM_USAGE_CI_TEST},
    {
        "name": "HF_TOKEN",
        "valueFrom": {
            "secretKeyRef": {
                "name": KubernetesConstants.HF_TOKEN_SECRET_NAME,
                "key": KubernetesConstants.HF_TOKEN_SECRET_KEY,
            }
        },
    },
]
DEFAULT_KUBERNETES_POD_VOLUMES = [
    {
        "name": KubernetesConstants.DEVSHM_VOLUME,
        "emptyDir": {"medium": KubernetesConstants.EMPTY_DIR_MEDIUM},
    },
    {
        "name": KubernetesConstants.HF_CACHE_VOLUME,
        "hostPath": {"path": HF_HOME, "type": KubernetesConstants.HOST_PATH_TYPE},
    },
]
DEFAULT_KUBERNETES_NODE_SELECTOR = {KubernetesConstants.NVIDIA_GPU_PRODUCT: KubernetesConstants.NVIDIA_A100_PRODUCT}


class DockerPluginConfig(BaseModel):
    """
    Configuration for Docker plugin running in a Buildkite step.
    The specification is based on:
    https://github.com/buildkite-plugins/docker-buildkite-plugin?tab=readme-ov-file#configuration
    """

    image: str = ""
    always_pull: bool = Field(default=True, alias="always-pull")
    propagate_environment: bool = Field(default=True, alias="propagate-environment")
    gpus: Optional[str] = "all"
    mount_buildkite_agent: Optional[bool] = Field(default=False, alias="mount-buildkite-agent")
    environment: List[str] = DEFAULT_DOCKER_ENVIRONMENT_VARIBLES
    volumes: List[str] = DEFAULT_DOCKER_VOLUMES
    shell: List[str] = ["/bin/bash", "-c"]


class KubernetesPodContainerConfig(BaseModel):
    """
    Configuration for a container running in a Kubernetes pod.
    """

    image: str
    resources: Dict[str, Dict[str, int]]
    volume_mounts: List[Dict[str, str]] = Field(
        alias="volumeMounts",
        default=DEFAULT_KUBERNETES_CONTAINER_VOLUME_MOUNTS,  # type: ignore
    )
    env: List[Dict[str, Any]] = Field(default_factory=lambda: list(DEFAULT_KUBERNETES_CONTAINER_ENVIRONMENT_VARIABLES))


class KubernetesPodSpec(BaseModel):
    """
    Configuration for a Kubernetes pod running in a Buildkite step.
    """

    containers: List[KubernetesPodContainerConfig]
    priority_class_name: str = Field(default="ci", alias="priorityClassName")
    node_selector: Dict[str, Any] = Field(default=DEFAULT_KUBERNETES_NODE_SELECTOR, alias="nodeSelector")
    volumes: List[Dict[str, Any]] = Field(default=DEFAULT_KUBERNETES_POD_VOLUMES)


class KubernetesPluginConfig(BaseModel):
    """
    Configuration for Kubernetes plugin running in a Buildkite step.
    """

    pod_spec: KubernetesPodSpec = Field(alias="podSpec")


def get_kubernetes_plugin_config(container_image: str, num_gpus: int) -> Dict:
    pod_spec = KubernetesPodSpec(
        containers=[
            KubernetesPodContainerConfig(
                image=container_image,
                resources={"limits": {KubernetesConstants.NVIDIA_GPU_RESOURCE: num_gpus}},
            )
        ]
    )
    return {KUBERNETES_PLUGIN_NAME: KubernetesPluginConfig(podSpec=pod_spec).model_dump(by_alias=True)}


def get_docker_plugin_config(docker_image_path: str, no_gpu: bool) -> Dict:
    docker_plugin_config = DockerPluginConfig(
        image=docker_image_path,
    )
    if no_gpu:
        docker_plugin_config.gpus = None
    return {DOCKER_PLUGIN_NAME: docker_plugin_config.model_dump(exclude_none=True, by_alias=True)}
