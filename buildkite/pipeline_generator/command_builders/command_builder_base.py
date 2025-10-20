"""Base classes for command transformation."""

from abc import ABC, abstractmethod
from typing import List, Optional


class CommandTransformer(ABC):
    """Base class for command transformers."""

    @abstractmethod
    def transform(self, commands: List[str], test_step, config) -> Optional[str]:
        """
        Transform commands into a single command string.

        Args:
            commands: List of commands to transform
            test_step: The test step being processed
            config: Pipeline configuration

        Returns:
            Single command string to execute, or None if transformation doesn't apply
        """
        pass
