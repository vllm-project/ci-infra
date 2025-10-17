"""Command transformation pipeline for test commands."""
from .base import CommandTransformer
from .normalizer import normalize_command, normalize_commands, flatten_commands
from .test_targeting import TestTargetingTransformer
from .coverage import CoverageTransformer, inject_coverage_into_commands

__all__ = [
    "CommandTransformer",
    "normalize_command",
    "normalize_commands",
    "flatten_commands",
    "TestTargetingTransformer",
    "CoverageTransformer",
    "inject_coverage_into_commands",
]
