"""CI mode test step generation - completely independent."""
from typing import List, Union

from ..models.buildkite_step import BuildkiteStep, BuildkiteBlockStep, get_step_key
from ..models.test_step import TestStep
from ..steps.test_steps import convert_test_step_to_buildkite_step
from ..selection.blocking import should_block_step


def generate_ci_test_steps(test_steps: List[TestStep], config) -> List[Union[BuildkiteStep, BuildkiteBlockStep]]:
    """Generate test steps for CI mode - no mode checks, pure CI logic."""
    steps: List[Union[BuildkiteStep, BuildkiteBlockStep]] = []
    
    for test_step in test_steps:
        # Skip fast_check_only tests
        if test_step.fast_check_only:
            continue
        
        # Determine if step needs a block
        if should_block_step(test_step, config):
            block_key = f"block-{get_step_key(test_step.label)}"
            steps.append(BuildkiteBlockStep(
                block=f"Run {test_step.label}",
                key=block_key,
                depends_on="image-build"
            ))
            depends_on = block_key
        else:
            depends_on = "image-build-cpu" if test_step.no_gpu else "image-build"
        
        # Convert test step using CI converter
        buildkite_step = convert_test_step_to_buildkite_step(
            test_step,
            config.container_image_cpu if test_step.no_gpu else config.container_image,
            config
        )
        buildkite_step.depends_on = depends_on
        steps.append(buildkite_step)
    
    return steps

