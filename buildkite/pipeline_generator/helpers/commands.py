"""Command normalization and flattening utilities."""

from typing import List, Union


def normalize_command(command: str) -> str:
    """Normalize a single command by removing YAML line continuations (backslashes)."""
    return command.replace(" \\", " ").replace("\\\n", " ").replace("\\", "")


def normalize_commands(commands: List[str]) -> List[str]:
    """Normalize a list of commands."""
    return [normalize_command(cmd) for cmd in commands]


def flatten_commands(commands: Union[List[str], List[List[str]]]) -> List[str]:
    """
    Flatten nested command lists into a simple list.
    For multi-node tests, returns only the first node's commands.
    """
    if not commands:
        return []
    
    # Check if it's a nested list (multi-node format)
    if isinstance(commands[0], list):
        return commands[0]  # type: ignore[return-value]
    
    return commands  # type: ignore[return-value]

