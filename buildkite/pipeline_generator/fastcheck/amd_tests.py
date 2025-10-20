"""Fastcheck-specific AMD test group generation."""

from typing import Any, Dict, List

from ..data_models.buildkite_step import get_step_key
from ..data_models.test_step import TestStep
from ..pipeline_config import PipelineGeneratorConfig
from ..utils.amd_command_builder import build_amd_test_command, format_amd_commands
from ..utils.constants import DEFAULT_WORKING_DIR, AMDLabelPrefixes, BuildStepKeys, EnvironmentVariables, TestLabels
from .docker_builds import generate_amd_build_step


def generate_amd_group(
    test_steps: List[TestStep], config: PipelineGeneratorConfig
) -> Dict[str, Any]:
    """Generate the AMD tests group for fastcheck (Basic Correctness Test only)."""
    amd_steps = []

    # Add AMD build step
    amd_build = generate_amd_build_step(config)
    amd_build_dict = amd_build.model_dump(exclude_none=True)
    # Fastcheck needs depends_on: null explicitly
    amd_build_dict["depends_on"] = None
    amd_steps.append(amd_build_dict)

    # Add AMD mirror tests - ONLY "Basic Correctness Test" in fastcheck
    for test_step in test_steps:
        # Skip tests that don't match mirror hardware
        if not test_step.mirror_hardwares or config.mirror_hw not in test_step.mirror_hardwares:
            continue

        # Fastcheck filter: only Basic Correctness Test
        if test_step.label != TestLabels.BASIC_CORRECTNESS_TEST:
            continue

        # Fastcheck adds a block for Basic Correctness Test
        block_key = f"block-amd-{get_step_key(test_step.label)}"
        amd_steps.append(
            {
                "block": f"Run AMD MI300: {
                    test_step.label} with {
                    config.mirror_hw}",
                "key": block_key,
                "depends_on": BuildStepKeys.AMD_BUILD,
            })

        # Format commands for AMD test
        commands_str = format_amd_commands(test_step)
        working_dir = test_step.working_dir or DEFAULT_WORKING_DIR
        full_command = build_amd_test_command(working_dir, commands_str)

        # Fastcheck mode: different label format, depends on block,
        # soft_fail=true, mi300_1 queue
        label = AMDLabelPrefixes.with_test_and_mirror(
            test_step.label, config.mirror_hw)

        amd_step_dict = {
            "label": label,
            "depends_on": block_key,
            "agents": {"queue": "amd_mi300_1"},  # Fastcheck uses mi300_1
            "env": {EnvironmentVariables.DOCKER_BUILDKIT: "1"},
            "soft_fail": True,  # Fastcheck uses soft_fail=true
            "priority": 100,
            "command": full_command,
        }
        amd_steps.append(amd_step_dict)

    return {"group": "AMD Tests", "depends_on": None, "steps": amd_steps}
