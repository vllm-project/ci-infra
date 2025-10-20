"""Fastcheck-specific test filtering logic."""

from ..data_models.test_step import TestStep
from ..pipeline_config import PipelineGeneratorConfig


def should_run_fastcheck_test(test_step: TestStep, config: PipelineGeneratorConfig) -> bool:
    """
    Determine if a fastcheck test should run.

    In fastcheck, this is simple: check the fast_check flag.
    """
    return bool(test_step.fast_check) if hasattr(test_step, "fast_check") else False
