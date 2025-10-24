"""Tests for utility functions."""

from typing import List

import pytest

from ..utils.agent_queues import get_agent_queue
from ..utils.command_utils import get_full_test_command, get_multi_node_test_command
from ..utils.constants import (
    DEFAULT_WORKING_DIR,
    MULTI_NODE_TEST_SCRIPT,
    TEST_DEFAULT_COMMANDS,
    AgentQueue,
    GPUType,
)

TEST_DEFAULT_COMMANDS_STR = ";\n".join(TEST_DEFAULT_COMMANDS)


class TestAgentQueue:
    """Tests for agent queue selection."""

    @pytest.mark.parametrize(
        ("no_gpu", "gpu_type", "num_gpus", "label", "expected_result"),
        [
            (True, None, None, None, AgentQueue.CPU_QUEUE_PREMERGE_US_EAST_1),
            (False, GPUType.A100.value, None, None, AgentQueue.A100_QUEUE),
            (False, GPUType.H100.value, None, None, AgentQueue.MITHRIL_H100_POOL),
            (False, GPUType.H200.value, None, None, AgentQueue.SKYLAB_H200),
            (False, GPUType.B200.value, None, None, AgentQueue.B200),
            (False, None, 1, None, AgentQueue.GPU_1_QUEUE),
            (False, None, 4, None, AgentQueue.GPU_4_QUEUE),
            (False, None, None, "Documentation Build", AgentQueue.SMALL_CPU_QUEUE_PREMERGE),
        ],
    )
    def test_get_agent_queue(self, no_gpu: bool, gpu_type: str, num_gpus: int, label: str, expected_result: AgentQueue):
        """Test agent queue selection based on requirements."""
        assert get_agent_queue(no_gpu, gpu_type, num_gpus, label) == expected_result

    def test_get_agent_queue_default_gpu(self):
        """Test default GPU agent queue when no specific requirements."""
        assert get_agent_queue(False, None, None, None) == AgentQueue.GPU_1_QUEUE


class TestFullTestCommand:
    """Tests for full test command generation."""

    @pytest.mark.parametrize(
        ("test_commands", "step_working_dir", "expected_result"),
        [
            (
                ["echo 'hello'"],
                None,
                f"{TEST_DEFAULT_COMMANDS_STR};\ncd {DEFAULT_WORKING_DIR};\necho 'hello'",
            ),
            (
                ["echo 'hello'"],
                "/vllm-workspace/tests",
                f"{TEST_DEFAULT_COMMANDS_STR};\ncd /vllm-workspace/tests;\necho 'hello'",
            ),
            (
                ["echo 'hello1'", "echo 'hello2'"],
                "/sample_tests",
                f"{TEST_DEFAULT_COMMANDS_STR};\ncd /sample_tests;\necho 'hello1';\necho 'hello2'",
            ),
        ],
    )
    def test_get_full_test_command(self, test_commands: List[str], step_working_dir: str, expected_result: str):
        """Test full command generation with working directory."""
        assert get_full_test_command(test_commands, step_working_dir) == expected_result

    def test_get_full_test_command_multiple_commands(self):
        """Test full command with multiple test commands."""
        commands = ["pytest test1.py", "pytest test2.py", "pytest test3.py"]
        result = get_full_test_command(commands, "/tests")

        assert "(command nvidia-smi || true)" in result
        assert "cd /tests" in result
        assert "pytest test1.py" in result
        assert "pytest test2.py" in result
        assert "pytest test3.py" in result


class TestMultiNodeTestCommand:
    """Tests for multi-node test command generation."""

    def test_get_multi_node_test_command(self):
        """Test multi-node command generation."""
        test_commands = [
            "distributed/test_same_node.py;pytest -v -s distributed/test_multi_node.py",
            "distributed/test_same_node.py",
        ]
        working_dir = "/vllm-workspace/tests"
        num_nodes = 2
        num_gpus = 4
        docker_image_path = "ecr-path/vllm-ci-test-repo:latest"

        expected_multi_node_command = [
            MULTI_NODE_TEST_SCRIPT,
            working_dir,
            num_nodes,
            num_gpus,
            docker_image_path,
            f"'{test_commands[0]}'",
            f"'{test_commands[1]}'",
        ]
        expected_result = " ".join(map(str, expected_multi_node_command))

        result = get_multi_node_test_command(test_commands, working_dir, num_nodes, num_gpus, docker_image_path)
        assert result == expected_result

    def test_get_multi_node_test_command_single_node_command(self):
        """Test multi-node with single command per node."""
        test_commands = ["pytest test1.py", "pytest test2.py"]
        result = get_multi_node_test_command(test_commands, "/tests", 2, 2, "test:latest")

        assert MULTI_NODE_TEST_SCRIPT in result
        assert "/tests" in result
        assert "'pytest test1.py'" in result
        assert "'pytest test2.py'" in result
        assert "test:latest" in result


class TestConstants:
    """Tests for constants."""

    def test_gpu_type_enum(self):
        """Test GPU type enum values."""
        assert GPUType.A100.value == "a100"
        assert GPUType.H100.value == "h100"
        assert GPUType.H200.value == "h200"
        assert GPUType.B200.value == "b200"

    def test_agent_queue_enum(self):
        """Test agent queue enum has expected values."""
        assert hasattr(AgentQueue, "CPU_QUEUE")
        assert hasattr(AgentQueue, "GPU_1_QUEUE")
        assert hasattr(AgentQueue, "GPU_4_QUEUE")
        assert hasattr(AgentQueue, "A100_QUEUE")
        assert hasattr(AgentQueue, "MITHRIL_H100_POOL")
        assert hasattr(AgentQueue, "SKYLAB_H200")

    def test_test_default_commands(self):
        """Test default commands are defined."""
        assert isinstance(TEST_DEFAULT_COMMANDS, list)
        assert len(TEST_DEFAULT_COMMANDS) > 0
        assert any("nvidia-smi" in cmd for cmd in TEST_DEFAULT_COMMANDS)

    def test_default_working_dir(self):
        """Test default working directory."""
        assert DEFAULT_WORKING_DIR == "/vllm-workspace/tests"
