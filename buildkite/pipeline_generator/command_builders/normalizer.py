"""Command normalization utilities."""

from typing import List, Union


def normalize_command(command: str) -> str:
    """
    Normalize a single command by removing YAML line continuations (backslashes).

    Args:
        command: Command string to normalize

    Returns:
        Normalized command string
    """
    # Remove backslashes used for YAML line continuations, preserving spaces
    return command.replace(" \\", " ").replace("\\\n", " ").replace("\\", "")


def normalize_commands(commands: List[str]) -> List[str]:
    """
    Normalize a list of commands.

    Args:
        commands: List of command strings

    Returns:
        List of normalized command strings
    """
    return [normalize_command(cmd) for cmd in commands]


def flatten_commands(commands: Union[List[str], List[List[str]]]) -> List[str]:
    """
    Flatten nested command lists into a simple list.

    For multi-node tests, commands might be a list of lists (one per node).
    This function returns only the first node's commands (not full flatten).

    Args:
        commands: Commands (can be nested for multi-node)

    Returns:
        Commands list (first node if multi-node)
    """
    if not commands:
        return []

    # Check if it's a nested list (multi-node format)
    if isinstance(commands[0], list):
        # Return only first node's commands (matching original behavior)
        return commands[0]  # type: ignore[return-value]

    # Already flat
    return commands  # type: ignore[return-value]
