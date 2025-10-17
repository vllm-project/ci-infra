"""
Pipeline generation modes - completely independent implementations.

Each mode has its own dedicated modules with zero shared conditional logic.
This ensures changes to one mode never break another.
"""
from .ci_mode import generate_ci_pipeline
from .fastcheck_mode import generate_fastcheck_pipeline

__all__ = [
    "generate_ci_pipeline",
    "generate_fastcheck_pipeline",
]

