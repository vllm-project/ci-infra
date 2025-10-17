"""Command utilities for test execution."""
from typing import List

from .constants import DEFAULT_WORKING_DIR, MULTI_NODE_TEST_SCRIPT, TEST_DEFAULT_COMMANDS


def get_full_test_command(test_commands: List[str], step_working_dir: str) -> str:
    """Convert test commands into one-line command with the right directory."""
    working_dir = step_working_dir or DEFAULT_WORKING_DIR
    test_commands_str = ";\n".join(test_commands)
    full_test_commands = [
        *TEST_DEFAULT_COMMANDS,
        f"cd {working_dir}",
        test_commands_str
    ]
    return ";\n".join(full_test_commands)


def get_multi_node_test_command(
        test_commands: List[str],
        working_dir: str,
        num_nodes: int,
        num_gpus: int,
        docker_image_path: str
        ) -> str:
    quoted_commands = [f"'{command}'" for command in test_commands]
    multi_node_command = [
        MULTI_NODE_TEST_SCRIPT,
        working_dir or DEFAULT_WORKING_DIR,
        str(num_nodes),
        str(num_gpus),
        docker_image_path,
        *quoted_commands
    ]
    return " ".join(map(str, multi_node_command))

