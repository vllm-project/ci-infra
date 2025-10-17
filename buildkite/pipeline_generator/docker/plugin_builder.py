"""Docker plugin builder for test steps."""
from typing import Dict

from ..models.docker_config import (
    StandardDockerConfig,
    SpecialGPUDockerConfig,
    DockerEnvironment,
    DockerVolumes,
    get_h100_kubernetes_config,
    get_a100_kubernetes_config,
    HF_HOME_FSX
)
from ..utils.constants import GPUType
from ..transformers.normalizer import flatten_commands, normalize_commands
from ..transformers.test_targeting import TestTargetingTransformer
from ..transformers.coverage import CoverageTransformer


def build_docker_command(test_step, config) -> str:
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
    commands = flatten_commands(test_step.commands)
    commands = normalize_commands(commands)
    
    # Try intelligent test targeting first
    targeting_transformer = TestTargetingTransformer()
    targeted_command = targeting_transformer.transform(commands, test_step, config)
    if targeted_command:
        return targeted_command
    
    # Apply coverage if enabled, otherwise just join commands
    coverage_transformer = CoverageTransformer()
    result = coverage_transformer.transform(commands, test_step, config)
    return result if result else " && ".join(commands)


def build_full_docker_command(test_step, config) -> str:
    """Build the full command that runs inside docker container."""
    docker_command = build_docker_command(test_step, config)
    working_dir = test_step.working_dir or "/vllm-workspace/tests"
    return f'(command nvidia-smi || true) && export VLLM_ALLOW_DEPRECATED_BEAM_SEARCH=1 && cd {working_dir} && {docker_command}'


def build_full_docker_command_no_coverage(test_step, config) -> str:
    """Build docker command without coverage injection (for kubernetes)."""
    # Flatten and normalize commands
    commands = flatten_commands(test_step.commands)
    commands = normalize_commands(commands)
    
    # Just join commands without coverage
    docker_command = " && ".join(commands)
    working_dir = test_step.working_dir or "/vllm-workspace/tests"
    return f'(command nvidia-smi || true) && export VLLM_ALLOW_DEPRECATED_BEAM_SEARCH=1 && cd {working_dir} && {docker_command}'


def build_docker_plugin(test_step, container_image: str, config) -> Dict:
    """Build standard Docker plugin configuration."""
    full_command = build_full_docker_command(test_step, config)
    # Docker plugin commands need trailing space to match jinja (line 237)
    if full_command and not full_command.endswith(' '):
        full_command += ' '
    bash_flags = "-xce" if config.fail_fast else "-xc"
    
    # Build environment configuration
    # Fastcheck mode doesn't include CODECOV_TOKEN or BUILDKITE_ANALYTICS_TOKEN
    from ..utils.constants import PipelineMode
    is_fastcheck = (config.pipeline_mode == PipelineMode.FASTCHECK)
    
    environment = DockerEnvironment(
        hf_home=HF_HOME_FSX,
        fail_fast=config.fail_fast,
        is_main_branch=(config.branch == "main" and not is_fastcheck),
        special_attention_backend=(test_step.label == "Speculative decoding tests"),
        skip_codecov=is_fastcheck
    )
    
    # Build volumes configuration
    volumes = DockerVolumes(hf_home=HF_HOME_FSX)
    
    # Determine if mount_buildkite_agent is needed
    mount_agent = (
        test_step.label == "Benchmarks" or 
        test_step.mount_buildkite_agent or 
        config.cov_enabled
    )
    
    docker_config = StandardDockerConfig(
        image=container_image,
        command=full_command,
        bash_flags=bash_flags,
        has_gpu=not test_step.no_gpu,
        environment=environment,
        volumes=volumes,
        mount_buildkite_agent=mount_agent
    )
    
    return docker_config.to_plugin_dict()


def build_special_gpu_plugin(test_step, container_image: str, config) -> Dict:
    """Build Docker plugin for special GPUs (H200, B200)."""
    full_command = build_full_docker_command(test_step, config)
    # Docker plugin commands need trailing space to match jinja
    if full_command and not full_command.endswith(' '):
        full_command += ' '
    bash_flags = "-xce" if config.fail_fast else "-xc"
    
    gpu_type = test_step.gpu.value if test_step.gpu else "h200"
    
    docker_config = SpecialGPUDockerConfig(
        image=container_image,
        command=full_command,
        bash_flags=bash_flags,
        gpu_type=gpu_type,
        fail_fast=config.fail_fast,
        is_main_branch=(config.branch == "main")
    )
    
    return docker_config.to_plugin_dict()


def build_kubernetes_plugin(test_step, container_image: str, config) -> Dict:
    """Build Kubernetes plugin for A100/H100."""
    # For kubernetes, build command WITHOUT coverage (jinja doesn't inject coverage for kubernetes)
    docker_command_no_cov = build_full_docker_command_no_coverage(test_step, config)
    num_gpus = test_step.num_gpus or 1
    
    if test_step.gpu == GPUType.H100:
        return get_h100_kubernetes_config(container_image, docker_command_no_cov, num_gpus)
    elif test_step.gpu == GPUType.A100:
        return get_a100_kubernetes_config(container_image, docker_command_no_cov, num_gpus)
    else:
        # Fallback to standard config
        return build_docker_plugin(test_step, container_image, config)


def build_plugin_for_test_step(test_step, container_image: str, config) -> Dict:
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

