"""Fastcheck mode test step generation - completely independent."""
from typing import List, Union

from ..models.buildkite_step import BuildkiteStep, BuildkiteBlockStep
from ..models.test_step import TestStep
from ..utils.constants import GPUType
from ..steps.fastcheck_steps import convert_fastcheck_test_step


def generate_fastcheck_test_steps(test_steps: List[TestStep], config) -> List[Union[BuildkiteStep, BuildkiteBlockStep]]:
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
        depends_on = "image-build"
        
        # Convert using fastcheck converter (always main image)
        buildkite_step = convert_fastcheck_test_step(
            test_step,
            config.container_image,
            config
        )
        buildkite_step.depends_on = depends_on
        steps.append(buildkite_step)
    
    return steps

