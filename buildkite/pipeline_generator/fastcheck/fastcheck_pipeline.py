"""Fastcheck mode pipeline generation."""

from typing import Any, Dict, List, Union

from ..ci.hardware_tests import generate_all_hardware_tests
from ..data_models.buildkite_step import BuildkiteBlockStep, BuildkiteStep, get_step_key
from ..data_models.test_step import TestStep
from ..pipeline_config import PipelineGeneratorConfig
from ..utils.constants import (
    AgentQueue,
    BlockLabels,
    BuildStepKeys,
    GPUType,
    HardwareLabels,
    PriorityValues,
    Scripts,
)
from .amd_tests import generate_amd_group
from .docker_builds import generate_main_build_step
from .hardware_tests import get_gh200_test, get_intel_tests, get_tpu_v0_tests, get_tpu_v1_tests
from .test_step_converter import convert_fastcheck_test_step


def generate_fastcheck_test_steps(test_steps: List[TestStep], config: PipelineGeneratorConfig) -> List[Union[BuildkiteStep, BuildkiteBlockStep]]:
    """
    Generate test steps for fastcheck mode - only fast_check tests.

    No mode checks needed - this is purely fastcheck logic.
    """
    steps: List[Union[BuildkiteStep, BuildkiteBlockStep]] = []

    for test_step in test_steps:
        # Only fast_check tests run immediately
        if not test_step.fast_check:
            continue

        # Skip multi-node and A100 (handled separately)
        if test_step.num_nodes and test_step.num_nodes >= 2:
            continue
        if test_step.gpu == GPUType.A100:
            continue

        # Fast check tests always run immediately (no blocks)
        depends_on = BuildStepKeys.MAIN_IMAGE

        # Convert using fastcheck converter (always main image)
        buildkite_step = convert_fastcheck_test_step(test_step, config.container_image, config)
        buildkite_step.depends_on = depends_on
        steps.append(buildkite_step)

    return steps


def _add_neuron_test(steps: List) -> None:
    """Add Neuron test at the beginning (fastcheck-specific)."""
    neuron_block: Dict[str, Any] = {
        "block": BlockLabels.RUN_NEURON_TEST,
        "depends_on": None,
        "key": "run-neuron-test",
    }
    neuron_test: Dict[str, Any] = {
        "label": HardwareLabels.NEURON_TEST,
        "depends_on": "run-neuron-test",
        "agents": {"queue": AgentQueue.NEURON},
        "command": f"bash {Scripts.RUN_NEURON_TEST}",
        "soft_fail": False,
    }
    steps.append(neuron_block)
    steps.append(neuron_test)


def generate_blocked_test_steps(test_steps: List[TestStep], config: PipelineGeneratorConfig) -> List[Union[BuildkiteStep, BuildkiteBlockStep, Dict[str, Any]]]:
    """Generate blocked steps for non-fast-check tests."""
    steps: List[Union[BuildkiteStep, BuildkiteBlockStep, Dict[str, Any]]] = []

    # Regular tests (not fast_check, not multi-node, not A100)
    for test_step in test_steps:
        if test_step.fast_check:
            continue
        if test_step.num_nodes and test_step.num_nodes >= 2:
            continue
        if test_step.gpu == GPUType.A100:
            continue

        block_key = f"block-{get_step_key(test_step.label)}"
        steps.append(
            BuildkiteBlockStep(
                block=f"Run {test_step.label}",
                key=block_key,
                depends_on=BuildStepKeys.MAIN_IMAGE,
            )
        )

        buildkite_step = convert_fastcheck_test_step(test_step, config.container_image, config)
        buildkite_step.depends_on = block_key
        steps.append(buildkite_step)

    # Multi-node tests (all blocked individually)
    for test_step in test_steps:
        if not (test_step.num_nodes and test_step.num_nodes >= 2):
            continue

        block_key = f"block-{get_step_key(test_step.label)}"
        steps.append(
            BuildkiteBlockStep(
                block=f"Run {test_step.label}",
                key=block_key,
                depends_on=BuildStepKeys.MAIN_IMAGE,
            )
        )

        buildkite_step = convert_fastcheck_test_step(test_step, config.container_image, config)
        buildkite_step.depends_on = block_key
        steps.append(buildkite_step)

    # A100 tests (all behind single block)
    a100_tests = [t for t in test_steps if t.gpu == GPUType.A100]
    if a100_tests:
        a100_block: Dict[str, Any] = {
            "block": BlockLabels.RUN_A100_TESTS,
            "depends_on": BuildStepKeys.MAIN_IMAGE,
        }
        steps.append(a100_block)  # type: ignore[arg-type]
        for test_step in a100_tests:
            buildkite_step = convert_fastcheck_test_step(test_step, config.container_image, config)
            buildkite_step.priority = PriorityValues.A100_TESTS
            steps.append(buildkite_step)

    return steps


def generate_fastcheck_pipeline(test_steps: List[TestStep], config: PipelineGeneratorConfig) -> List[Union[BuildkiteStep, BuildkiteBlockStep, Dict[str, Any]]]:
    """Generate complete fastcheck pipeline."""
    steps: List[Union[BuildkiteStep, BuildkiteBlockStep, Dict[str, Any]]] = []

    # Build step (main image only)
    steps.append(generate_main_build_step(config))

    # Neuron test (at the top, before regular tests)
    _add_neuron_test(steps)

    # Fast-check tests (run immediately)
    steps.extend(generate_fastcheck_test_steps(test_steps, config))

    # Non-fast-check tests (blocked)
    steps.extend(generate_blocked_test_steps(test_steps, config))

    # Hardware tests in specific order required by Jinja template
    # Order: TPU V0, TPU V1, GH200, AMD Tests, Intel CPU, Intel GPU

    # Get base hardware tests for Intel extraction
    all_hw_tests = generate_all_hardware_tests(config.branch, config.nightly)

    # Add in specific order (matches Jinja template layout)
    steps.extend(get_tpu_v0_tests())
    steps.extend(get_tpu_v1_tests(all_hw_tests))
    steps.extend(get_gh200_test())
    steps.append(generate_amd_group(test_steps, config))
    steps.extend(get_intel_tests(all_hw_tests))

    return steps
