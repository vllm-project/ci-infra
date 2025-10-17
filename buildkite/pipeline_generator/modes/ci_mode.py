"""CI mode pipeline generation."""
from typing import List, Union, Dict, Any

from ..models.buildkite_step import BuildkiteStep, BuildkiteBlockStep
from ..models.test_step import TestStep
from ..steps.build_steps import (
    generate_main_build_step,
    generate_cu118_build_steps,
    generate_cpu_build_step
)
from ..steps.group_steps import generate_amd_group, generate_torch_nightly_group
from ..steps.hardware_steps import generate_all_hardware_tests
from .ci_test_steps import generate_ci_test_steps


def generate_ci_pipeline(test_steps: List[TestStep], config) -> List[Union[BuildkiteStep, BuildkiteBlockStep, Dict[str, Any]]]:
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

