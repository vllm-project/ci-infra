"""Tests for data models."""

import pytest
from pydantic import ValidationError

from ..data_models.buildkite_step import (
    BuildkiteBlockStep,
    BuildkiteStep,
    get_block_step,
    get_step_key,
)
from ..data_models.test_step import DEFAULT_TEST_WORKING_DIR, TestStep
from ..utils.constants import AgentQueue, GPUType


class TestStepKey:
    """Tests for step key generation."""

    @pytest.mark.parametrize(
        ("step_label", "expected_result"),
        [
            ("Test Step", "test-step"),
            ("Test Step 2", "test-step-2"),
            ("Test (Step)", "test-step"),
            # Commas are replaced with dashes
            ("Test A, B, C", "test-a--b--c"),
            ("Distributed Tests (50%)", "distributed-tests-50"),
            ("LoRA Test +Plus", "lora-test--plus"),
        ],
    )
    def test_get_step_key(self, step_label: str, expected_result: str):
        assert get_step_key(step_label) == expected_result


class TestBlockStep:
    """Tests for block step generation."""

    @pytest.mark.parametrize(
        ("step_label", "expected_block", "expected_key"),
        [
            ("Test Step", "Run Test Step", "block-test-step"),
            ("Test Step 2", "Run Test Step 2", "block-test-step-2"),
            ("Test (Step)", "Run Test (Step)", "block-test-step"),
        ],
    )
    def test_get_block_step(self, step_label: str, expected_block: str, expected_key: str):
        block_step = get_block_step(step_label)
        assert block_step.block == expected_block
        assert block_step.key == expected_key


class TestTestStep:
    """Tests for TestStep model."""

    def test_create_test_step_with_command(self):
        """Test creating a test step with command field."""
        test_step = TestStep(
            label="Test Step",
            command="echo 'hello'",
        )
        assert test_step.label == "Test Step"
        assert test_step.working_dir == DEFAULT_TEST_WORKING_DIR
        assert test_step.optional is False
        assert test_step.commands == ["echo 'hello'"]
        assert test_step.command is None  # Converted to commands

    def test_create_test_step_with_commands(self):
        """Test creating a test step with commands list."""
        test_step = TestStep(
            label="Test Step",
            commands=["echo 'hello1'", "echo 'hello2'"],
        )
        assert test_step.label == "Test Step"
        assert test_step.commands == ["echo 'hello1'", "echo 'hello2'"]

    def test_create_test_step_fail_duplicate_command(self):
        """Test that providing both command and commands fails."""
        with pytest.raises(ValueError, match="Only one of 'command' or 'commands'"):
            TestStep(
                label="Test Step",
                command="echo 'hello'",
                commands=["echo 'hello'"],
            )

    def test_create_test_step_fail_no_command(self):
        """Test that providing neither command nor commands fails."""
        with pytest.raises(ValueError, match="Either 'command' or 'commands' must be defined"):
            TestStep(label="Test Step")

    def test_create_test_step_fail_gpu_and_no_gpu(self):
        """Test that providing both gpu and no_gpu fails."""
        with pytest.raises(ValueError, match="cannot be defined together"):
            TestStep(
                label="Test Step",
                command="echo 'hello'",
                gpu=GPUType.A100,
                no_gpu=True,
            )

    def test_create_test_step_with_gpu(self):
        """Test creating a test step with GPU requirements."""
        test_step = TestStep(label="Test Step", command="echo 'hello'", gpu=GPUType.A100, num_gpus=2)
        assert test_step.gpu == GPUType.A100
        assert test_step.num_gpus == 2

    def test_create_test_step_fail_invalid_gpu(self):
        """Test that invalid GPU type fails validation."""
        with pytest.raises(ValidationError):
            TestStep(
                label="Test Step",
                command="echo 'hello'",
                gpu="invalid_gpu_type",
            )

    def test_create_test_step_multi_node_missing_num_gpus(self):
        """Test that multi-node without num_gpus fails."""
        with pytest.raises(ValueError, match="'num_gpus' must be defined if 'num_nodes' is defined"):
            TestStep(
                label="Test Step",
                command="echo 'hello'",
                num_nodes=2,
            )

    def test_create_test_step_multi_node_mismatched_commands(self):
        """Test that multi-node with wrong number of command lists fails."""
        with pytest.raises(ValueError, match="Number of command lists must match the number of nodes"):
            TestStep(
                label="Test Step",
                num_nodes=2,
                num_gpus=2,
                commands=[["echo 'hello1'"], ["echo 'hello2'"], ["echo 'hello3'"]],
            )

    def test_create_test_step_multi_node_valid(self):
        """Test creating a valid multi-node test step."""
        test_step = TestStep(
            label="Test Step",
            num_nodes=2,
            num_gpus=2,
            commands=[["echo 'hello1'"], ["echo 'hello2'"]],
        )
        assert test_step.label == "Test Step"
        assert test_step.num_nodes == 2
        assert test_step.num_gpus == 2
        assert test_step.commands == [["echo 'hello1'"], ["echo 'hello2'"]]

    def test_create_test_step_with_dependencies(self):
        """Test creating a test step with source file dependencies."""
        test_step = TestStep(
            label="Test Step",
            command="pytest test.py",
            source_file_dependencies=["vllm/engine/", "tests/test_engine.py"],
        )
        assert test_step.source_file_dependencies == ["vllm/engine/", "tests/test_engine.py"]

    def test_create_test_step_optional(self):
        """Test creating an optional test step."""
        test_step = TestStep(
            label="Optional Test",
            command="pytest test.py",
            optional=True,
        )
        assert test_step.optional is True


class TestBuildkiteStep:
    """Tests for BuildkiteStep model."""

    def test_create_buildkite_step(self):
        """Test creating a basic Buildkite step."""
        buildkite_step = BuildkiteStep(
            label="Test Step",
            key="test-step",
            commands=["echo 'hello'"],
        )
        assert buildkite_step.label == "Test Step"
        assert buildkite_step.key == "test-step"
        assert buildkite_step.agents == {"queue": AgentQueue.CPU_QUEUE}
        assert buildkite_step.commands == ["echo 'hello'"]

    def test_create_buildkite_step_fail_no_commands(self):
        """Test that creating a step without commands fails."""
        with pytest.raises(ValidationError):
            BuildkiteStep(
                label="Test Step",
                key="test-step",
            )

    def test_create_buildkite_step_with_custom_agent(self):
        """Test creating a step with custom agent queue."""
        buildkite_step = BuildkiteStep(
            label="Test Step",
            commands=["echo 'hello'"],
            agents={"queue": AgentQueue.GPU_1_QUEUE},
        )
        assert buildkite_step.agents == {"queue": AgentQueue.GPU_1_QUEUE}

    def test_create_buildkite_step_fail_invalid_agent_queue(self):
        """Test that invalid agent queue fails validation."""
        with pytest.raises(ValidationError, match="Invalid agent queue"):
            BuildkiteStep(
                label="Test Step",
                commands=["echo 'hello'"],
                agents={"queue": "invalid-queue"},
            )

    def test_create_buildkite_step_with_plugins(self):
        """Test creating a step with plugins."""
        buildkite_step = BuildkiteStep(label="Test Step", commands=[], plugins=[{"docker#v5.2.0": {"image": "test:latest"}}])
        assert buildkite_step.plugins == [{"docker#v5.2.0": {"image": "test:latest"}}]

    def test_create_buildkite_step_with_retry(self):
        """Test creating a step with retry configuration."""
        buildkite_step = BuildkiteStep(
            label="Test Step",
            commands=["echo 'hello'"],
            retry={"automatic": [{"exit_status": -1, "limit": 1}]},
        )
        assert buildkite_step.retry == {"automatic": [{"exit_status": -1, "limit": 1}]}


class TestBuildkiteBlockStep:
    """Tests for BuildkiteBlockStep model."""

    def test_create_block_step(self):
        """Test creating a basic block step."""
        block_step = BuildkiteBlockStep(block="Run Test", key="block-test")
        assert block_step.block == "Run Test"
        assert block_step.key == "block-test"
        assert block_step.depends_on is None

    def test_create_block_step_with_depends_on(self):
        """Test creating a block step with dependencies."""
        block_step = BuildkiteBlockStep(block="Run Test", key="block-test", depends_on="image-build")
        assert block_step.depends_on == "image-build"
