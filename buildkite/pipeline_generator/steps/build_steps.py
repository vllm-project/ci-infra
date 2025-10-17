"""Build step generation for Docker images using data-driven configuration."""
from typing import List, Union, Optional

from ..models.buildkite_step import BuildkiteStep, BuildkiteBlockStep
from ..utils.constants import AgentQueue, PipelineMode
from ..build_config import (
    create_main_cuda_build,
    create_fastcheck_build,
    create_cu118_build,
    create_cpu_build,
    create_torch_nightly_build,
    AMDBuildConfig
)


def generate_main_build_step(config) -> BuildkiteStep:
    """Build the main Docker CUDA image."""
    # Fastcheck uses different build function
    if config.pipeline_mode == PipelineMode.FASTCHECK:
        build_config = create_fastcheck_build(
            commit=config.commit,
            image_tag=config.container_image,
            queue=AgentQueue.AWS_CPU_PREMERGE_SIMPLE.value,
            vllm_use_precompiled=config.vllm_use_precompiled
        )
    else:
        # CI mode - original logic
        queue = AgentQueue.AWS_CPU_POSTMERGE if config.branch == "main" else AgentQueue.AWS_CPU_PREMERGE
        build_config = create_main_cuda_build(
            commit=config.commit,
            image_tag=config.container_image,
            queue=queue.value,
            branch=config.branch,
            vllm_use_precompiled=config.vllm_use_precompiled
        )
    
    build_dict = build_config.to_buildkite_step()
    return BuildkiteStep(**build_dict)


def generate_cu118_build_steps(config) -> List[Union[BuildkiteStep, BuildkiteBlockStep]]:
    """Build the CUDA 11.8 Docker image."""
    queue = AgentQueue.AWS_CPU_POSTMERGE if config.branch == "main" else AgentQueue.AWS_CPU_PREMERGE
    
    block_step = BuildkiteBlockStep(
        block="Build CUDA 11.8 image",
        key="block-build-cu118",
        depends_on=None
    )
    
    build_config = create_cu118_build(
        commit=config.commit,
        image_tag=config.container_image_cu118,
        queue=queue.value,
        branch=config.branch,
        vllm_use_precompiled=config.vllm_use_precompiled
    )
    
    build_dict = build_config.to_buildkite_step()
    build_step = BuildkiteStep(**build_dict)
    
    return [block_step, build_step]


def generate_cpu_build_step(config) -> BuildkiteStep:
    """Build the CPU Docker image."""
    queue = AgentQueue.AWS_CPU_POSTMERGE if config.branch == "main" else AgentQueue.AWS_CPU_PREMERGE
    
    build_config = create_cpu_build(
        commit=config.commit,
        image_tag=config.container_image_cpu,
        queue=queue.value
    )
    
    build_dict = build_config.to_buildkite_step()
    return BuildkiteStep(**build_dict)


def generate_torch_nightly_build_step(config, depends_on: Optional[str]) -> BuildkiteStep:
    """Build the torch nightly Docker image."""
    queue = AgentQueue.AWS_CPU_POSTMERGE if config.branch == "main" else AgentQueue.AWS_CPU_PREMERGE
    
    build_config = create_torch_nightly_build(
        commit=config.commit,
        image_tag=config.container_image_torch_nightly,
        queue=queue.value,
        depends_on=depends_on
    )
    
    build_dict = build_config.to_buildkite_step()
    # Add torch nightly specific overrides
    build_dict["soft_fail"] = True
    build_dict["timeout_in_minutes"] = 360
    
    return BuildkiteStep(**build_dict)


def generate_amd_build_step(config) -> BuildkiteStep:
    """Build the AMD Docker image."""
    amd_config = AMDBuildConfig(
        image_tag=config.container_image_amd,
        commit=config.commit,
        mirror_hw=config.mirror_hw,
        is_fastcheck=(config.pipeline_mode == PipelineMode.FASTCHECK)
    )
    
    build_dict = amd_config.to_buildkite_step()
    return BuildkiteStep(**build_dict)
