"""CI-specific torch nightly test group generation."""

from typing import Any, Dict, List

from ..data_models.buildkite_step import BuildkiteBlockStep, get_step_key
from ..data_models.test_step import TestStep
from ..pipeline_config import PipelineGeneratorConfig
from ..utils.constants import BuildStepKeys
from .docker_builds import generate_torch_nightly_build_step
from .manual_trigger_rules import should_block_torch_nightly_test
from .test_step_converter import convert_test_step_to_buildkite_step


def generate_torch_nightly_group(test_steps: List[TestStep], config: PipelineGeneratorConfig) -> Dict[str, Any]:
    """Generate the torch nightly group (CI-only)."""
    torch_nightly_steps = []

    # Add block step UNLESS nightly mode
    if not config.nightly:
        block_step = BuildkiteBlockStep(block="Build torch nightly image", key="block-build-torch-nightly", depends_on=None)
        torch_nightly_steps.append(block_step.model_dump(exclude_none=True))

    # Add build step for torch nightly
    depends_on = "block-build-torch-nightly" if not config.nightly else None
    torch_build = generate_torch_nightly_build_step(config, depends_on)
    torch_nightly_steps.append(torch_build.model_dump(exclude_none=True))

    # Add test steps
    for test_step in test_steps:
        if not test_step.torch_nightly:
            continue

        # Determine if step needs a block (using torch nightly blocking rules)
        if should_block_torch_nightly_test(test_step, config):
            block_key = f"block-torch-nightly-{get_step_key(test_step.label)}"
            block_step = BuildkiteBlockStep(
                block=f"Run Torch Nightly {test_step.label}",
                key=block_key,
                depends_on=BuildStepKeys.TORCH_NIGHTLY_IMAGE,
            )
            torch_nightly_steps.append(block_step.model_dump(exclude_none=True))
            depends_on = block_key
        else:
            depends_on = BuildStepKeys.TORCH_NIGHTLY_IMAGE

        # Convert test step to buildkite step
        buildkite_step = convert_test_step_to_buildkite_step(test_step, config.container_image_torch_nightly, config)
        buildkite_step.label = f"Torch Nightly {test_step.label}"
        buildkite_step.depends_on = depends_on
        buildkite_step.soft_fail = True
        torch_nightly_steps.append(buildkite_step.model_dump(exclude_none=True))

    return {"group": "vllm against torch nightly", "depends_on": None, "steps": torch_nightly_steps}
