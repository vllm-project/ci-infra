import pytest
import sys
from pydantic import ValidationError

from buildkite.pipeline_generator.step import get_step_key, get_block_step, BuildkiteBlockStep, TestStep, DEFAULT_TEST_WORKING_DIR, BuildkiteStep
from buildkite.pipeline_generator.utils import AgentQueue, GPUType

@pytest.mark.parametrize(
    ("step_label", "expected_result"),
    [
        ("Test Step", "test-step"),
        ("Test Step 2", "test-step-2"),
        ("Test (Step)", "test-step"),
        ("Test A, B, C", "test-a-b-c"),
    ],
)
def test_get_step_key(step_label: str, expected_result: str):
    assert get_step_key(step_label) == expected_result


@pytest.mark.parametrize(
    ("step_label", "expected_result"),
    [
        ("Test Step", BuildkiteBlockStep(block="Run Test Step", key="block-test-step")),
        ("Test Step 2", BuildkiteBlockStep(block="Run Test Step 2", key="block-test-step-2")),
        ("Test (Step)", BuildkiteBlockStep(block="Run Test (Step)", key="block-test-step")),
        ("Test A, B, C", BuildkiteBlockStep(block="Run Test A, B, C", key="block-test-a-b-c")),
    ],
)
def test_get_block_step(step_label: str, expected_result: BuildkiteBlockStep):
    assert get_block_step(step_label) == expected_result

def test_create_test_step_with_command():
    test_step = TestStep(
        label="Test Step",
        command="echo 'hello'",
    )
    assert test_step.label == "Test Step"
    # Check default values
    assert test_step.working_dir == DEFAULT_TEST_WORKING_DIR
    assert test_step.optional is False
    assert test_step.commands == ["echo 'hello'"]
    assert test_step.command is None


def test_create_test_step_fail_duplicate_command():
    with pytest.raises(ValueError):
        test_step = TestStep(
            label="Test Step",
            command="echo 'hello'",
            commands=["echo 'hello'"],
        )

def test_create_test_step_fail_gpu_and_no_gpu():
    with pytest.raises(ValueError, match="cannot be defined together"):
        test_step = TestStep(
            label="Test Step",
            command="echo 'hello'",
            gpu="a100",
            no_gpu=True,
        )

def test_create_test_step_fail_gpu():
    with pytest.raises(ValidationError):
        test_step = TestStep(
            label="Test Step",
            command="echo 'hello'",
            gpu="abc100",
        )

def test_create_test_step_multi_node():
    with pytest.raises(ValueError, match="'num_gpus' must be defined if 'num_nodes' is defined."):
        test_step = TestStep(
            label="Test Step",
            command="echo 'hello'",
            num_nodes=2,
        )
    
    with pytest.raises(ValueError, match="Number of commands must match the number of nodes."):
        test_step = TestStep(
            label="Test Step",
            num_nodes=2,
            num_gpus=2,
            commands=["echo 'hello1'", "echo 'hello2'", "echo 'hello3'"],
        )
    
    test_step = TestStep(
        label="Test Step",
        num_nodes=2,
        num_gpus=2,
        commands=["echo 'hello1'", "echo 'hello2'"],
    )
    assert test_step.label == "Test Step"
    assert test_step.num_nodes == 2
    assert test_step.num_gpus == 2
    assert test_step.commands == ["echo 'hello1'", "echo 'hello2'"]

def test_create_buildkite_step():
    buildkite_step = BuildkiteStep(
        label="Test Step",
        key="test-step",
        commands = ["echo 'hello'"],
    )
    assert buildkite_step.label == "Test Step"
    assert buildkite_step.key == "test-step"
    assert buildkite_step.agents == {"queue": AgentQueue.AWS_CPU}
    assert buildkite_step.depends_on == "build"

def test_create_buildkite_step_fail_no_command():
    with pytest.raises(ValidationError):
        buildkite_step = BuildkiteStep(
            label="Test Step",
            key="test-step",
        )

def test_create_buildkite_step_fail_wrong_agent_queue():
    with pytest.raises(ValidationError):
        buildkite_step = BuildkiteStep(
            label="Test Step",
            key="test-step",
            agents={"queue": "wrong-queue"},
        )


if __name__ == "__main__":
    sys.exit(pytest.main(["-v", __file__]))
