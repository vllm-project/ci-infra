"""Fastcheck mode pipeline generation."""
from typing import List, Union, Dict, Any

from ..models.buildkite_step import BuildkiteStep, BuildkiteBlockStep, get_step_key
from ..models.test_step import TestStep
from ..utils.constants import GPUType
from ..steps.build_steps import generate_main_build_step
from ..steps.group_steps import generate_amd_group
from ..steps.fastcheck_steps import convert_fastcheck_test_step
from ..steps.fastcheck_hardware_steps import generate_fastcheck_hardware_tests
from .fastcheck_test_steps import generate_fastcheck_test_steps
from ..shared.job_labels import HardwareLabels, BlockLabels
from ..shared.script_paths import Scripts


def _add_neuron_test_at_top(steps: List) -> None:
    """Add Neuron test at the beginning (fastcheck-specific)."""
    neuron_block: Dict[str, Any] = {
        "block": BlockLabels.RUN_NEURON_TEST,
        "depends_on": None,
        "key": "run-neuron-test"
    }
    neuron_test: Dict[str, Any] = {
        "label": HardwareLabels.NEURON_TEST,
        "depends_on": "run-neuron-test",
        "agents": {"queue": "neuron"},
        "command": f"bash {Scripts.RUN_NEURON_TEST}",
        "soft_fail": False
    }
    steps.append(neuron_block)
    steps.append(neuron_test)


def generate_blocked_test_steps(test_steps: List[TestStep], config) -> List[Union[BuildkiteStep, BuildkiteBlockStep, Dict[str, Any]]]:
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
        steps.append(BuildkiteBlockStep(
            block=f"Run {test_step.label}",
            key=block_key,
            depends_on="image-build"
        ))
        
        buildkite_step = convert_fastcheck_test_step(test_step, config.container_image, config)
        buildkite_step.depends_on = block_key
        steps.append(buildkite_step)
    
    # Multi-node tests (all blocked individually)
    for test_step in test_steps:
        if not (test_step.num_nodes and test_step.num_nodes >= 2):
            continue
        
        block_key = f"block-{get_step_key(test_step.label)}"
        steps.append(BuildkiteBlockStep(
            block=f"Run {test_step.label}",
            key=block_key,
            depends_on="image-build"
        ))
        
        buildkite_step = convert_fastcheck_test_step(test_step, config.container_image, config)
        buildkite_step.depends_on = block_key
        steps.append(buildkite_step)
    
    # A100 tests (all behind single block)
    a100_tests = [t for t in test_steps if t.gpu == GPUType.A100]
    if a100_tests:
        a100_block: Dict[str, Any] = {
            "block": BlockLabels.RUN_A100_TESTS,
            "depends_on": "image-build"
        }
        steps.append(a100_block)  # type: ignore[arg-type]
        for test_step in a100_tests:
            buildkite_step = convert_fastcheck_test_step(test_step, config.container_image, config)
            buildkite_step.priority = 10000
            steps.append(buildkite_step)
    
    return steps


def generate_fastcheck_pipeline(test_steps: List[TestStep], config) -> List[Union[BuildkiteStep, BuildkiteBlockStep, Dict[str, Any]]]:
    """Generate complete fastcheck pipeline."""
    steps: List[Union[BuildkiteStep, BuildkiteBlockStep, Dict[str, Any]]] = []
    
    # Build step (main image only)
    steps.append(generate_main_build_step(config))
    
    # Neuron test (at the top, before regular tests)
    _add_neuron_test_at_top(steps)
    
    # Fast-check tests (run immediately)
    steps.extend(generate_fastcheck_test_steps(test_steps, config))
    
    # Non-fast-check tests (blocked)
    steps.extend(generate_blocked_test_steps(test_steps, config))
    
    # Hardware tests and AMD group (interleaved order)
    fastcheck_hw = generate_fastcheck_hardware_tests(config.branch, config.nightly)
    steps.extend(fastcheck_hw[:8])  # TPU V0, TPU V1, GH200
    steps.append(generate_amd_group(test_steps, config))
    steps.extend(fastcheck_hw[8:])  # Intel CPU, GPU
    
    return steps

