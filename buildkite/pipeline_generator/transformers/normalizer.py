"""Command normalization utilities."""
from typing import List


def normalize_command(cmd: str) -> str:
    """Normalize command string to match jinja rendering.
    
    Removes backslash characters from line continuations in source YAML.
    Jinja template removes backslashes, leaving the spaces that follow them.
    """
    # Remove backslash characters (jinja behavior)
    return cmd.replace('\\', '')


def normalize_commands(commands: List[str]) -> List[str]:
    """Normalize a list of commands."""
    return [normalize_command(cmd) for cmd in commands]


def flatten_commands(commands):
    """Flatten multi-node commands (list of lists) to a simple list."""
    if not commands:
        return []
    if isinstance(commands[0], list):
        # Multi-node: flatten to first node's commands for single-node execution
        return commands[0]
    return commands

