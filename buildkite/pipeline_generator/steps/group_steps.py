"""Group generation for special test categories (AMD, Torch Nightly)."""
from typing import List, Dict, Any, Optional

from ..models.buildkite_step import BuildkiteBlockStep, get_step_key
from ..models.test_step import TestStep
from ..selection.blocking import should_block_step
from .test_steps import convert_test_step_to_buildkite_step
from .build_steps import generate_amd_build_step, generate_torch_nightly_build_step
from ..shared.job_labels import TestLabels, AMDQueueLabels


def get_amd_queue(label: str, num_gpus: Optional[int] = None) -> str:
    """
    Determine AMD queue based on test label.
    
    Maps test labels to AMD GPU counts (8, 4, 2, or 1 GPUs).
    Based on test-template-ci.j2 lines 673-681.
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


def generate_amd_group(test_steps: List[TestStep], config) -> Dict[str, Any]:
    """Generate the AMD tests group."""
    # Fastcheck mode only includes "Basic Correctness Test"
    from ..utils.constants import PipelineMode
    is_fastcheck = (config.pipeline_mode == PipelineMode.FASTCHECK)
    
    amd_steps = []
    
    # Add AMD build step using data class
    amd_build = generate_amd_build_step(config)
    amd_build_dict = amd_build.dict(exclude_none=True)
    # Fastcheck needs depends_on: null explicitly
    if is_fastcheck:
        amd_build_dict["depends_on"] = None
    amd_steps.append(amd_build_dict)
    
    # Add AMD mirror tests
    for test_step in test_steps:
        if test_step.mirror_hardwares and config.mirror_hw in test_step.mirror_hardwares:
            # Fastcheck filter: only Basic Correctness Test
            if is_fastcheck and test_step.label != TestLabels.BASIC_CORRECTNESS_TEST:
                continue
            
            # Fastcheck adds a block for Basic Correctness Test
            if is_fastcheck and test_step.label == TestLabels.BASIC_CORRECTNESS_TEST:
                block_key = f"block-amd-{get_step_key(test_step.label)}"
                amd_steps.append({
                    "block": f"Run AMD MI300: {test_step.label} with {config.mirror_hw}",
                    "key": block_key,
                    "depends_on": "amd-build"
                })
            
            # Determine queue based on label
            queue = get_amd_queue(test_step.label, test_step.num_gpus)
            
            # Handle commands - match jinja's join behavior
            raw_commands = test_step.commands or []
            
            if test_step.command:
                # Use command field if provided
                commands_str = test_step.command
            elif raw_commands and len(raw_commands) > 0 and isinstance(raw_commands[0], list):
                # Multi-node: jinja joins list of lists as string representation
                # step.commands | join(" && ") where commands is [[cmd1, cmd2], [cmd3]]
                # becomes: '["cmd1", "cmd2"] && ["cmd3"]'
                import json
                commands_str = " && ".join([json.dumps(node_cmds) for node_cmds in raw_commands])
            else:
                # Simple list of commands
                commands_list: List[str] = raw_commands  # type: ignore[assignment]
                commands_str = " && ".join(commands_list)
            working_dir = test_step.working_dir or "/vllm-workspace/tests"
            
            # AMD steps use 'command' field, not 'commands'
            # Build as dict to avoid Pydantic validation requiring commands
            
            # Fastcheck uses different queue and label format
            if is_fastcheck:
                from ..shared.job_labels import AMDLabelPrefixes
                label = AMDLabelPrefixes.with_test_and_mirror(test_step.label, config.mirror_hw)
                depends_on = f"block-amd-{get_step_key(test_step.label)}"
                queue_name = "amd_mi300_1"  # Fastcheck uses mi300_1 for Basic Correctness
                soft_fail_value = True  # Fastcheck uses true
            else:
                from ..shared.job_labels import AMDLabelPrefixes
                label = AMDLabelPrefixes.with_test(test_step.label)
                depends_on = "amd-build"
                queue_name = queue
                soft_fail_value = False
            
            amd_step_dict = {
                "label": label,
                "depends_on": depends_on,
                "agents": {"queue": queue_name},
                "env": {"DOCKER_BUILDKIT": "1"},
                "soft_fail": soft_fail_value,
                "priority": 100,
                "command": f'bash .buildkite/scripts/hardware_ci/run-amd-test.sh "(command rocm-smi || true) && export VLLM_ALLOW_DEPRECATED_BEAM_SEARCH=1 && cd {working_dir} ; {commands_str}"'
            }
            amd_steps.append(amd_step_dict)
    
    return {
        "group": "AMD Tests",
        "depends_on": None,
        "steps": amd_steps
    }


def generate_torch_nightly_group(test_steps: List[TestStep], config) -> Dict[str, Any]:
    """Generate the torch nightly group."""
    torch_nightly_steps = []
    
    # Add block step UNLESS nightly mode (jinja line 550-554)
    # The block appears even in run_all mode, only skipped in nightly mode
    if not config.nightly:  # Changed from "if not config.nightly" to be explicit
        block_step = BuildkiteBlockStep(
            block="Build torch nightly image",
            key="block-build-torch-nightly",
            depends_on=None
        )
        torch_nightly_steps.append(block_step.dict(exclude_none=True))
    
    # Add build step for torch nightly using data class
    depends_on = "block-build-torch-nightly" if not config.nightly else None
    torch_build = generate_torch_nightly_build_step(config, depends_on)
    torch_nightly_steps.append(torch_build.dict(exclude_none=True))
    
    # Add test steps
    for test_step in test_steps:
        if not test_step.torch_nightly:
            continue
        
        # Determine if step needs a block (pass is_torch_nightly_group=True)
        if should_block_step(test_step, config, is_torch_nightly_group=True):
            block_key = f"block-torch-nightly-{get_step_key(test_step.label)}"
            block_step = BuildkiteBlockStep(
                block=f"Run Torch Nightly {test_step.label}",
                key=block_key,
                depends_on="image-build-torch-nightly"
            )
            torch_nightly_steps.append(block_step.dict(exclude_none=True))
            depends_on = block_key
        else:
            depends_on = "image-build-torch-nightly"
        
        # Convert test step to buildkite step
        buildkite_step = convert_test_step_to_buildkite_step(
            test_step,
            config.container_image_torch_nightly,
            config
        )
        buildkite_step.label = f"Torch Nightly {test_step.label}"
        buildkite_step.depends_on = depends_on
        buildkite_step.soft_fail = True
        torch_nightly_steps.append(buildkite_step.dict(exclude_none=True))
    
    return {
        "group": "vllm against torch nightly",
        "depends_on": None,
        "steps": torch_nightly_steps
    }
