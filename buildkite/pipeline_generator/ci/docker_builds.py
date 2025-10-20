"""CI-specific Docker build step generation."""

from typing import List, Optional, Union

from ..data_models.buildkite_step import BuildkiteBlockStep, BuildkiteStep
from ..docker_build_configs import (
    AMDBuildConfig,
    create_cpu_build,
    create_cu118_build,
    create_main_cuda_build,
    create_torch_nightly_build,
)
from ..pipeline_config import PipelineGeneratorConfig
from ..utils.constants import AgentQueue


def generate_main_build_step(config: PipelineGeneratorConfig) -> BuildkiteStep:
    """Build the main Docker CUDA image for CI."""
    queue = AgentQueue.AWS_CPU_POSTMERGE if config.branch == "main" else AgentQueue.AWS_CPU_PREMERGE
    build_config = create_main_cuda_build(
        commit=config.commit,
        image_tag=config.container_image,
        queue=queue.value,
        branch=config.branch,
        vllm_use_precompiled=config.vllm_use_precompiled,
    )

    build_dict = build_config.to_buildkite_step()
    return BuildkiteStep(**build_dict)


def generate_cu118_build_steps(
    config: PipelineGeneratorConfig,
) -> List[Union[BuildkiteStep, BuildkiteBlockStep]]:
    """Build the CUDA 11.8 Docker image (CI only)."""
    queue = AgentQueue.AWS_CPU_POSTMERGE if config.branch == "main" else AgentQueue.AWS_CPU_PREMERGE

    block_step = BuildkiteBlockStep(
        block="Build CUDA 11.8 image", key="block-build-cu118", depends_on=None
    )

    build_config = create_cu118_build(
        commit=config.commit,
        image_tag=config.container_image_cu118,
        queue=queue.value,
        branch=config.branch,
        vllm_use_precompiled=config.vllm_use_precompiled,
    )

    build_dict = build_config.to_buildkite_step()
    build_step = BuildkiteStep(**build_dict)

    return [block_step, build_step]


def generate_cpu_build_step(config: PipelineGeneratorConfig) -> BuildkiteStep:
    """Build the CPU Docker image (CI only)."""
    queue = AgentQueue.AWS_CPU_POSTMERGE if config.branch == "main" else AgentQueue.AWS_CPU_PREMERGE

    build_config = create_cpu_build(
        commit=config.commit,
        image_tag=config.container_image_cpu,
        queue=queue.value)

    build_dict = build_config.to_buildkite_step()
    return BuildkiteStep(**build_dict)


def generate_torch_nightly_build_step(
    config: PipelineGeneratorConfig, depends_on: Optional[str]
) -> BuildkiteStep:
    """Build the torch nightly Docker image (CI only)."""
    queue = AgentQueue.AWS_CPU_POSTMERGE if config.branch == "main" else AgentQueue.AWS_CPU_PREMERGE

    build_config = create_torch_nightly_build(
        commit=config.commit,
        image_tag=config.container_image_torch_nightly,
        queue=queue.value,
        depends_on=depends_on,
    )

    build_dict = build_config.to_buildkite_step()
    # Add torch nightly specific overrides
    build_dict["soft_fail"] = True
    build_dict["timeout_in_minutes"] = 360

    return BuildkiteStep(**build_dict)


def generate_amd_build_step(config: PipelineGeneratorConfig) -> BuildkiteStep:
    """Build the AMD Docker image for CI."""
    amd_config = AMDBuildConfig(
        image_tag=config.container_image_amd,
        commit=config.commit,
        mirror_hw=config.mirror_hw,
        is_fastcheck=False,  # CI mode
    )

    build_dict = amd_config.to_buildkite_step()
    return BuildkiteStep(**build_dict)
