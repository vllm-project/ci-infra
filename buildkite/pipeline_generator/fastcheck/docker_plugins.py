"""Fastcheck-specific Docker plugin builder."""

from typing import Dict

from ..command_builders.normalizer import flatten_commands, normalize_commands
from ..data_models.docker_config import (
    HF_HOME,
    HF_HOME_FSX,
    DockerEnvironment,
    DockerVolumes,
    StandardDockerConfig,
)
from ..data_models.test_step import TestStep
from ..pipeline_config import PipelineGeneratorConfig
from ..utils.constants import (
    DEFAULT_WORKING_DIR,
    EnvironmentValues,
    EnvironmentVariables,
    KubernetesConstants,
    PluginNames,
    ShellCommands,
    TestLabels,
)


def build_fastcheck_environment(test_step: TestStep, config: PipelineGeneratorConfig) -> DockerEnvironment:
    """Build environment for fastcheck mode (no CODECOV_TOKEN, no BUILDKITE_ANALYTICS_TOKEN)."""
    return DockerEnvironment(
        hf_home=HF_HOME_FSX,
        fail_fast=config.fail_fast,
        is_main_branch=False,  # Never add BUILDKITE_ANALYTICS_TOKEN in fastcheck
        special_attention_backend=(test_step.label == TestLabels.SPECULATIVE_DECODING_TESTS),
        skip_codecov=True,  # No CODECOV_TOKEN in fastcheck
    )


def build_fastcheck_docker_command(test_step: TestStep, config: PipelineGeneratorConfig) -> str:
    """Build docker command for fastcheck mode (simpler, no coverage)."""
    # Flatten and normalize commands
    commands = flatten_commands(test_step.commands or [])
    commands = normalize_commands(commands)

    # Fastcheck doesn't use coverage or intelligent targeting - just join
    # commands
    docker_command = " && ".join(commands)
    working_dir = test_step.working_dir or DEFAULT_WORKING_DIR
    return f"{ShellCommands.CHECK_NVIDIA_GPU} && {ShellCommands.SETUP_DEPRECATED_BEAM_SEARCH} && cd {working_dir} && {docker_command}"


def build_fastcheck_docker_plugin(test_step, container_image: str, config: PipelineGeneratorConfig) -> Dict:
    """Build Docker plugin specifically for fastcheck mode."""
    full_command = build_fastcheck_docker_command(test_step, config)
    # Fastcheck template does NOT add trailing space (unlike CI template)
    bash_flags = "-xce" if config.fail_fast else "-xc"

    # Build environment configuration (fastcheck-specific)
    environment = build_fastcheck_environment(test_step, config)

    # Build volumes configuration
    volumes = DockerVolumes(hf_home=HF_HOME_FSX)

    # Determine if mount_buildkite_agent is needed
    mount_agent = test_step.label == TestLabels.BENCHMARKS or test_step.mount_buildkite_agent

    docker_config = StandardDockerConfig(
        image=container_image,
        command=full_command,
        bash_flags=bash_flags,
        has_gpu=not test_step.no_gpu,
        environment=environment,
        volumes=volumes,
        mount_buildkite_agent=mount_agent,
    )

    return docker_config.to_plugin_dict()


def build_fastcheck_a100_kubernetes_plugin(test_step, container_image: str) -> Dict:
    """Build Kubernetes plugin for A100 in fastcheck mode."""
    # Build command without coverage
    commands = flatten_commands(test_step.commands)
    commands = normalize_commands(commands)
    docker_command = " && ".join(commands)
    working_dir = test_step.working_dir or DEFAULT_WORKING_DIR
    full_command = f"{ShellCommands.CHECK_NVIDIA_GPU} && {ShellCommands.SETUP_DEPRECATED_BEAM_SEARCH} && cd {working_dir} && {docker_command}"

    num_gpus = test_step.num_gpus or 1

    # Fastcheck uses command/args format (not command with bash -c)
    pod_spec = {
        "priorityClassName": KubernetesConstants.PRIORITY_CLASS_CI,
        "containers": [
            {
                "image": container_image,
                "command": ["bash"],
                "args": ["-c", f"'{full_command}'"],
                "resources": {"limits": {KubernetesConstants.NVIDIA_GPU_RESOURCE: num_gpus}},
                "volumeMounts": [
                    {
                        "name": KubernetesConstants.DEVSHM_VOLUME,
                        "mountPath": KubernetesConstants.DEV_SHM_PATH,
                    },
                    {"name": KubernetesConstants.HF_CACHE_VOLUME, "mountPath": HF_HOME},
                ],
                "env": [
                    {
                        "name": EnvironmentVariables.VLLM_USAGE_SOURCE,
                        "value": EnvironmentValues.VLLM_USAGE_CI_TEST,
                    },
                    {"name": "NCCL_CUMEM_HOST_ENABLE", "value": 0},  # Integer
                    {"name": "HF_HOME", "value": HF_HOME},
                    {
                        "name": "HF_TOKEN",
                        "valueFrom": {
                            "secretKeyRef": {
                                "name": KubernetesConstants.HF_TOKEN_SECRET_NAME,
                                "key": KubernetesConstants.HF_TOKEN_SECRET_KEY,
                            }
                        },
                    },
                ],
            }
        ],
        "nodeSelector": {KubernetesConstants.NVIDIA_GPU_PRODUCT: KubernetesConstants.NVIDIA_A100_PRODUCT},
        "volumes": [
            {
                "name": KubernetesConstants.DEVSHM_VOLUME,
                "emptyDir": {"medium": KubernetesConstants.EMPTY_DIR_MEDIUM},
            },
            {
                "name": KubernetesConstants.HF_CACHE_VOLUME,
                "hostPath": {"path": HF_HOME, "type": KubernetesConstants.HOST_PATH_TYPE},
            },
        ],
    }

    return {PluginNames.KUBERNETES: {"podSpec": pod_spec}}
