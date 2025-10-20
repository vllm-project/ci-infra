"""Helper functions for building AMD test commands."""

import json
from typing import List

from .constants import Scripts, ShellCommands


def format_amd_commands(test_step) -> str:  # type: ignore[no-untyped-def]
    """
    Format commands for AMD tests, handling different command formats.

    Handles:
    - Single `command` field
    - Multi-node commands (list of lists)
    - Simple command lists

    Returns:
        Formatted command string ready for AMD test script
    """
    # Use command field if provided
    if test_step.command:
        return str(test_step.command)

    raw_commands = test_step.commands or []

    # Multi-node format: list of lists -> JSON string representation
    if raw_commands and isinstance(raw_commands[0], list):
        return " && ".join([json.dumps(node_cmds)
                           for node_cmds in raw_commands])

    # Simple list of commands
    commands_list: List[str] = raw_commands  # type: ignore[assignment]
    return " && ".join(commands_list)


def build_amd_test_command(working_dir: str, commands_str: str) -> str:
    """
    Build the complete AMD test command with proper wrapping.

    Args:
        working_dir: Test working directory
        commands_str: Formatted command string from format_amd_commands()

    Returns:
        Complete command ready for AMD test execution
    """
    inner_cmd = f"{ShellCommands.CHECK_AMD_GPU} && {ShellCommands.SETUP_DEPRECATED_BEAM_SEARCH} && cd {working_dir} ; {commands_str}"
    return f'bash {Scripts.RUN_AMD_TEST} "{inner_cmd}"'
