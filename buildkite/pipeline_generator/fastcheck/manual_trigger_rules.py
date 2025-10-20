"""Fastcheck-specific manual trigger (blocking) rules."""

from ..data_models.test_step import TestStep
from ..pipeline_config import PipelineGeneratorConfig


def should_block_fastcheck_test(
        test_step: TestStep,
        config: PipelineGeneratorConfig) -> bool:
    """
    Determine if a fastcheck test needs a manual trigger block.

    In fastcheck mode:
    - Tests with fast_check=True NEVER blocked (run immediately)
    - All non-fast-check tests ARE blocked
    """
    # Fast-check tests never blocked
    if test_step.fast_check:
        return False

    # All other tests are blocked
    return True
