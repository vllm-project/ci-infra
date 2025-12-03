"""Shared test fixtures and configuration."""

import pytest

from ..config import PipelineGeneratorConfig, PipelineMode
from ..models import TestStep

TEST_COMMIT = "abcdef0123456789abcdef0123456789abcdef01"
TEST_CONTAINER_REGISTRY = "container.registry"
TEST_CONTAINER_REGISTRY_REPO = "test"


@pytest.fixture
def pipeline_config():
    """Create a basic pipeline configuration for testing."""
    return PipelineGeneratorConfig(
        container_registry=TEST_CONTAINER_REGISTRY,
        container_registry_repo=TEST_CONTAINER_REGISTRY_REPO,
        commit=TEST_COMMIT,
        branch="main",
        list_file_diff=[],
        run_all=False,
        nightly=False,
        pipeline_mode=PipelineMode.CI,
    )


@pytest.fixture
def simple_test_step():
    """Create a simple test step for testing."""
    return TestStep(
        label="Test Step",
        commands=["pytest -v test_sample.py"],
        working_dir="/vllm-workspace/tests",
    )


@pytest.fixture
def multi_node_test_step():
    """Create a multi-node test step for testing."""
    return TestStep(
        label="Multi-Node Test",
        commands=[["pytest test1.py"], ["pytest test2.py"]],
        num_nodes=2,
        num_gpus=4,
        working_dir="/vllm-workspace/tests",
    )


@pytest.fixture
def gpu_test_step():
    """Create a GPU test step for testing."""
    return TestStep(label="GPU Test", commands=["pytest -v test_gpu.py"], gpu="a100", num_gpus=2)


@pytest.fixture
def optional_test_step():
    """Create an optional test step for testing."""
    return TestStep(
        label="Optional Test",
        commands=["pytest -v test_optional.py"],
        optional=True,
        source_file_dependencies=["vllm/engine/engine.py"],
    )
