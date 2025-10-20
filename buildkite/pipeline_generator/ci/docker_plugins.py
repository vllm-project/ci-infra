"""Docker plugin builder for test steps."""

from typing import Dict

from ..command_builders.coverage_injection import CoverageTransformer
from ..command_builders.intelligent_test_selection import TestTargetingTransformer
from ..command_builders.normalizer import flatten_commands, normalize_commands
from ..data_models.docker_config import (
    HF_HOME_FSX,
    DockerEnvironment,
    DockerVolumes,
    SpecialGPUDockerConfig,
    StandardDockerConfig,
    get_a100_kubernetes_config,
    get_h100_kubernetes_config,
)
from ..data_models.test_step import TestStep
from ..pipeline_config import PipelineGeneratorConfig
from ..utils.constants import DEFAULT_WORKING_DIR, GPUType, ShellCommands, TestLabels


def build_docker_command(test_step: TestStep,
                         config: PipelineGeneratorConfig) -> str:
    """
    Build docker command with command transformation pipeline.

    Applies transformations in order:
    1. Flatten multi-node commands
    2. Normalize commands (remove backslashes)
    3. Apply intelligent test targeting (if applicable)
    4. Apply coverage injection (if enabled)
    5. Join commands
    """
    # Flatten and normalize commands
    commands = flatten_commands(test_step.commands or [])
    commands = normalize_commands(commands)

    # Try intelligent test targeting first
    targeting_transformer = TestTargetingTransformer()
    targeted_command = targeting_transformer.transform(
        commands, test_step, config)
    if targeted_command:
        return targeted_command

    # Apply coverage if enabled, otherwise just join commands
    coverage_transformer = CoverageTransformer()
    result = coverage_transformer.transform(commands, test_step, config)
    return result if result else " && ".join(commands)


def build_full_docker_command(
        test_step: TestStep,
        config: PipelineGeneratorConfig) -> str:
    """Build the full command that runs inside docker container."""
    docker_command = build_docker_command(test_step, config)
    working_dir = test_step.working_dir or DEFAULT_WORKING_DIR
    return f"{ShellCommands.CHECK_NVIDIA_GPU} && {ShellCommands.SETUP_DEPRECATED_BEAM_SEARCH} && cd {working_dir} && {docker_command}"


def build_full_docker_command_no_coverage(
    test_step: TestStep, config: PipelineGeneratorConfig
) -> str:
    """Build docker command without coverage injection (for kubernetes)."""
    # Flatten and normalize commands
    commands = flatten_commands(test_step.commands or [])
    commands = normalize_commands(commands)

    # Just join commands without coverage
    docker_command = " && ".join(commands)
    working_dir = test_step.working_dir or DEFAULT_WORKING_DIR
    return f"{ShellCommands.CHECK_NVIDIA_GPU} && {ShellCommands.SETUP_DEPRECATED_BEAM_SEARCH} && cd {working_dir} && {docker_command}"


def build_docker_plugin(
    test_step: TestStep, container_image: str, config: PipelineGeneratorConfig
) -> Dict:
    """Build standard Docker plugin configuration."""
    full_command = build_full_docker_command(test_step, config)
    # Docker plugin commands need trailing space to match CI template
    # expectations
    if full_command and not full_command.endswith(" "):
        full_command += " "
    bash_flags = "-xce" if config.fail_fast else "-xc"

    # Build environment configuration
    # CI mode always includes CODECOV_TOKEN and BUILDKITE_ANALYTICS_TOKEN on
    # main branch
    environment = DockerEnvironment(
        hf_home=HF_HOME_FSX,
        fail_fast=config.fail_fast,
        is_main_branch=(config.branch == "main"),  # CI mode
        special_attention_backend=(
            test_step.label == TestLabels.SPECULATIVE_DECODING_TESTS),
        skip_codecov=False,  # CI mode always includes coverage token
    )

    # Build volumes configuration
    volumes = DockerVolumes(hf_home=HF_HOME_FSX)

    # Determine if mount_buildkite_agent is needed
    mount_agent = (test_step.label ==
                   TestLabels.BENCHMARKS or test_step.mount_buildkite_agent or config.cov_enabled)

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


def build_special_gpu_plugin(
    test_step: TestStep, container_image: str, config: PipelineGeneratorConfig
) -> Dict:
    """Build Docker plugin for special GPUs (H200, B200)."""
    full_command = build_full_docker_command(test_step, config)
    # Docker plugin commands need trailing space to match jinja
    if full_command and not full_command.endswith(" "):
        full_command += " "
    bash_flags = "-xce" if config.fail_fast else "-xc"

    gpu_type = test_step.gpu.value if test_step.gpu else "h200"

    docker_config = SpecialGPUDockerConfig(
        image=container_image,
        command=full_command,
        bash_flags=bash_flags,
        gpu_type=gpu_type,
        fail_fast=config.fail_fast,
        is_main_branch=(config.branch == "main"),
    )

    return docker_config.to_plugin_dict()


def build_kubernetes_plugin(
    test_step: TestStep, container_image: str, config: PipelineGeneratorConfig
) -> Dict:
    """Build Kubernetes plugin for A100/H100."""
    # For kubernetes, build command WITHOUT coverage (jinja doesn't inject
    # coverage for kubernetes)
    docker_command_no_cov = build_full_docker_command_no_coverage(
        test_step, config)
    num_gpus = test_step.num_gpus or 1

    # Route to GPU-specific config
    if test_step.gpu == GPUType.H100:
        return get_h100_kubernetes_config(
            container_image, docker_command_no_cov, num_gpus)

    if test_step.gpu == GPUType.A100:
        return get_a100_kubernetes_config(
            container_image, docker_command_no_cov, num_gpus)

    # Fallback to standard Docker plugin
    return build_docker_plugin(test_step, container_image, config)


def build_plugin_for_test_step(
    test_step: TestStep, container_image: str, config: PipelineGeneratorConfig
) -> Dict:
    """
    Build the appropriate plugin configuration for a test step.

    Routes to the correct plugin builder based on GPU type:
    - H200/B200: Special GPU plugin
    - H100/A100: Kubernetes plugin
    - Others: Standard Docker plugin
    """
    # Special GPUs (H200, B200)
    if test_step.gpu in [GPUType.H200, GPUType.B200]:
        return build_special_gpu_plugin(test_step, container_image, config)

    # Kubernetes for H100/A100
    if test_step.gpu in [GPUType.H100, GPUType.A100]:
        return build_kubernetes_plugin(test_step, container_image, config)

    # Standard Docker
    return build_docker_plugin(test_step, container_image, config)
