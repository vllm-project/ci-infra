"""Intelligent test targeting transformer."""

from typing import List, Optional

from ..ci.test_filtering import (
    are_only_tests_changed,
    extract_pytest_markers,
    get_changed_tests,
    get_intelligent_test_targets,
)
from .command_builder_base import CommandTransformer


class TestTargetingTransformer(CommandTransformer):
    """Transformer that applies intelligent test targeting when only tests changed."""

    def transform(
            self,
            commands: List[str],
            test_step,
            config) -> Optional[str]:
        """
        Transform commands to target specific tests when only test files changed.

        Returns the targeted command if applicable, None otherwise.
        """
        if not are_only_tests_changed(config.list_file_diff):
            return None

        changed_tests = get_changed_tests(config.list_file_diff)
        matched_targets = get_intelligent_test_targets(
            test_step, changed_tests)

        if not matched_targets:
            return None

        # Build targeted pytest command
        markers = extract_pytest_markers(commands)
        return f"pytest -v -s{markers} {' '.join(matched_targets)}"
