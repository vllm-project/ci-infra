"""AMD mode pipeline generation - only AMD tests."""

from typing import Any, Dict, List

from ..config import *  # noqa: F403, F405
from ..helpers.builds import build_amd_image
from ..helpers.commands import flatten_commands


def get_amd_queue(label: str) -> str:
    """Determine AMD queue based on label."""
    if label in AMD_MI325_8_LABELS:
        return QUEUE_AMD_MI325_8
    elif label in AMD_MI325_4_LABELS:
        return QUEUE_AMD_MI325_4
    elif label in AMD_MI325_2_LABELS:
        return QUEUE_AMD_MI325_2
    else:
        return QUEUE_AMD_MI325_1


def generate_amd_pipeline(test_steps, config) -> List[Dict[str, Any]]:
    """
    Generate AMD-only pipeline.
    Returns a single AMD test group containing build + all AMD tests.
    """
    group_steps = []
    
    # Add AMD build
    group_steps.append(build_amd_image(config))
    
    # Add all AMD mirror tests
    for test in test_steps:
        if not test.mirror_hardwares or config.mirror_hw not in test.mirror_hardwares:
            continue
        
        # Format commands for AMD
        commands = flatten_commands(test.commands or [])
        commands_str = " && ".join(commands)
        working_dir = test.working_dir or DEFAULT_WORKING_DIR
        
        full_command = f"(command rocm-smi || true) && cd {working_dir} && {commands_str}"
        
        step = {
            "label": f"AMD MI300: {test.label}",
            "depends_on": BUILD_KEY_AMD,
            "agents": {"queue": get_amd_queue(test.label)},
            "env": {"DOCKER_BUILDKIT": "1"},
            "soft_fail": False,
            "priority": PRIORITY_AMD,
            "command": full_command,
        }
        
        group_steps.append(step)
    
    # Return as a single-item list containing the AMD group
    return [{
        "group": "AMD Tests",
        "depends_on": None,
        "steps": group_steps,
    }]

