import pytest
import sys
from typing import List

from buildkite.pipeline_generator.utils import (
    get_agent_queue,
    get_full_test_command,
    get_multi_node_test_command,
    AgentQueue,
    MULTI_NODE_TEST_SCRIPT,
    TEST_DEFAULT_COMMANDS,
)

TEST_DEFAULT_COMMANDS_STR = ";\n".join(TEST_DEFAULT_COMMANDS)

@pytest.mark.parametrize(
    ("no_gpu", "gpu_type", "num_gpus", "expected_result"),
    [
        (True, None, None, AgentQueue.AWS_SMALL_CPU),
        (False, "a100", None, AgentQueue.A100),
        (False, None, 1, AgentQueue.AWS_1xL4),
        (False, None, 4, AgentQueue.AWS_4xL4),
    ],
)
def test_get_agent_queue(no_gpu: bool, gpu_type: str, num_gpus: int, expected_result: AgentQueue):
    assert get_agent_queue(no_gpu, gpu_type, num_gpus) == expected_result


@pytest.mark.parametrize(
    ("test_commands", "step_working_dir", "expected_result"),
    [
        (["echo 'hello'"], None, f"{TEST_DEFAULT_COMMANDS_STR};\ncd /vllm-workspace/tests;\necho 'hello'"),
        (["echo 'hello'"], "/vllm-workspace/tests", f"{TEST_DEFAULT_COMMANDS_STR};\ncd /vllm-workspace/tests;\necho 'hello'"),
        (["echo 'hello1'", "echo 'hello2'"], "/sample_tests", f"{TEST_DEFAULT_COMMANDS_STR};\ncd /sample_tests;\necho 'hello1';\necho 'hello2'"),
    ],
)
def test_get_full_test_command(test_commands: List[str], step_working_dir: str, expected_result: str):
    assert get_full_test_command(test_commands, step_working_dir) == expected_result


def test_get_multi_node_test_command():
    test_commands = [
        (
            "distributed/test_same_node.py;"
            "pytest -v -s distributed/test_multi_node_assignment.py;"
            "pytest -v -s distributed/test_pipeline_parallel.py"
        ),
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
    assert get_multi_node_test_command(test_commands, working_dir, num_nodes, num_gpus, docker_image_path) == expected_result


if __name__ == "__main__":
    sys.exit(pytest.main(["-v", __file__]))
