"""Fastcheck-specific Docker build step generation."""

from ..data_models.buildkite_step import BuildkiteStep
from ..docker_build_configs import AMDBuildConfig, create_fastcheck_build
from ..pipeline_config import PipelineGeneratorConfig
from ..utils.constants import AgentQueue


def generate_main_build_step(config: PipelineGeneratorConfig) -> BuildkiteStep:
    """Build the main Docker image for fastcheck (single build only)."""
    build_config = create_fastcheck_build(
        commit=config.commit,
        image_tag=config.container_image,
        queue=AgentQueue.CPU_QUEUE_PREMERGE,
        vllm_use_precompiled=config.vllm_use_precompiled,
    )

    build_dict = build_config.to_buildkite_step()
    return BuildkiteStep(**build_dict)


def generate_amd_build_step(config: PipelineGeneratorConfig) -> BuildkiteStep:
    """Build the AMD Docker image for fastcheck."""
    amd_config = AMDBuildConfig(
        image_tag=config.container_image_amd,
        commit=config.commit,
        mirror_hw=config.mirror_hw,
        is_fastcheck=True,  # Fastcheck mode
    )

    build_dict = amd_config.to_buildkite_step()
    return BuildkiteStep(**build_dict)
