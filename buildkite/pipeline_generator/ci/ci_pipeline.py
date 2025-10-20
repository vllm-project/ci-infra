"""CI mode pipeline generation."""

from typing import Any, Dict, List, Union

from ..data_models.buildkite_step import BuildkiteBlockStep, BuildkiteStep, get_step_key
from ..data_models.test_step import TestStep
from ..pipeline_config import PipelineGeneratorConfig
from ..utils.constants import BuildStepKeys
from .amd_tests import generate_amd_group
from .docker_builds import (
    generate_cpu_build_step,
    generate_cu118_build_steps,
    generate_main_build_step,
)
from .hardware_tests import generate_all_hardware_tests
from .manual_trigger_rules import should_block_ci_test
from .test_step_converter import convert_test_step_to_buildkite_step
from .torch_nightly_tests import generate_torch_nightly_group


def _create_ci_test_with_optional_block(test_step: TestStep, config: PipelineGeneratorConfig) -> List[Union[BuildkiteStep, BuildkiteBlockStep]]:
    """
    Create test step with optional blocking step.

    Returns a list containing [block_step, test_step] or just [test_step].
    """
    # Determine container image
    container_image = config.container_image_cpu if test_step.no_gpu else config.container_image

    # Convert test step
    buildkite_step = convert_test_step_to_buildkite_step(test_step, container_image, config)

    # Add blocking step if needed
    if should_block_ci_test(test_step, config):
        block_key = f"block-{get_step_key(test_step.label)}"
        block_step = BuildkiteBlockStep(
            block=f"Run {test_step.label}",
            key=block_key,
            depends_on=BuildStepKeys.MAIN_IMAGE,
        )
        buildkite_step.depends_on = block_key
        return [block_step, buildkite_step]

    # No blocking - depend on base image
    base_dependency = BuildStepKeys.CPU_IMAGE if test_step.no_gpu else BuildStepKeys.MAIN_IMAGE
    buildkite_step.depends_on = base_dependency
    return [buildkite_step]


def generate_ci_test_steps(test_steps: List[TestStep], config: PipelineGeneratorConfig) -> List[Union[BuildkiteStep, BuildkiteBlockStep]]:
    """Generate test steps for CI mode - no mode checks, pure CI logic."""
    steps: List[Union[BuildkiteStep, BuildkiteBlockStep]] = []

    for test_step in test_steps:
        # Skip fast_check_only tests
        if test_step.fast_check_only:
            continue

        # Create test (with optional block) and add to steps
        steps.extend(_create_ci_test_with_optional_block(test_step, config))

    return steps


def generate_ci_pipeline(test_steps: List[TestStep], config: PipelineGeneratorConfig) -> List[Union[BuildkiteStep, BuildkiteBlockStep, Dict[str, Any]]]:
    """Generate complete CI pipeline."""
    steps: List[Union[BuildkiteStep, BuildkiteBlockStep, Dict[str, Any]]] = []

    # Build steps (main, cu118, cpu)
    steps.append(generate_main_build_step(config))
    steps.extend(generate_cu118_build_steps(config))
    steps.append(generate_cpu_build_step(config))

    # Test steps
    steps.extend(generate_ci_test_steps(test_steps, config))

    # Torch nightly group
    steps.append(generate_torch_nightly_group(test_steps, config))

    # AMD tests group
    steps.append(generate_amd_group(test_steps, config))

    # Hardware tests
    steps.extend(generate_all_hardware_tests(config.branch, config.nightly))

    return steps
