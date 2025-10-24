"""Command transformation pipeline for test commands."""

from .command_builder_base import CommandTransformer
from .coverage_injection import CoverageTransformer, inject_coverage_into_commands
from .intelligent_test_selection import TestTargetingTransformer
from .normalizer import flatten_commands, normalize_command, normalize_commands

__all__ = [
    "CommandTransformer",
    "normalize_command",
    "normalize_commands",
    "flatten_commands",
    "TestTargetingTransformer",
    "CoverageTransformer",
    "inject_coverage_into_commands",
]
