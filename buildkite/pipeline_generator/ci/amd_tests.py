"""CI-specific AMD test group generation."""

from typing import Any, Dict, List, Optional

from ..data_models.test_step import TestStep
from ..pipeline_config import PipelineGeneratorConfig
from ..utils.amd_command_builder import build_amd_test_command, format_amd_commands
from ..utils.constants import DEFAULT_WORKING_DIR, AMDLabelPrefixes, AMDQueueLabels, BuildStepKeys, EnvironmentVariables
from .docker_builds import generate_amd_build_step


def get_amd_queue(label: str, num_gpus: Optional[int] = None) -> str:
    """
    Determine AMD queue based on test label.

    Maps test labels to AMD GPU counts (8, 4, 2, or 1 GPUs).
    """
    if label in AMDQueueLabels.AMD_MI325_8_LABELS:
        return "amd_mi325_8"
    elif label in AMDQueueLabels.AMD_MI325_4_LABELS:
        return "amd_mi325_4"
    elif label in AMDQueueLabels.AMD_MI325_2_LABELS:
        return "amd_mi325_2"
    else:
        # Default: 1 GPU
        return "amd_mi325_1"


def generate_amd_group(
    test_steps: List[TestStep], config: PipelineGeneratorConfig
) -> Dict[str, Any]:
    """Generate the AMD tests group for CI (all AMD tests)."""
    amd_steps = []

    # Add AMD build step
    amd_build = generate_amd_build_step(config)
    amd_build_dict = amd_build.model_dump(exclude_none=True)
    amd_steps.append(amd_build_dict)

    # Add AMD mirror tests - ALL tests matching mirror_hw
    for test_step in test_steps:
        # Skip tests that don't match mirror hardware
        if not test_step.mirror_hardwares or config.mirror_hw not in test_step.mirror_hardwares:
            continue

        # Determine queue based on label
        queue = get_amd_queue(test_step.label, test_step.num_gpus)

        # Format commands for AMD test
        commands_str = format_amd_commands(test_step)
        working_dir = test_step.working_dir or DEFAULT_WORKING_DIR
        full_command = build_amd_test_command(working_dir, commands_str)

        # CI mode: simple label, depends on amd-build, soft_fail=false
        label = AMDLabelPrefixes.with_test(test_step.label)

        amd_step_dict = {
            "label": label,
            "depends_on": BuildStepKeys.AMD_BUILD,
            "agents": {"queue": queue},
            "env": {EnvironmentVariables.DOCKER_BUILDKIT: "1"},
            "soft_fail": False,
            "priority": 100,
            "command": full_command,
        }
        amd_steps.append(amd_step_dict)

    return {"group": "AMD Tests", "depends_on": None, "steps": amd_steps}
