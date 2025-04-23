import pytest
import sys
import os
import tempfile
import yaml

from buildkite.pipeline_generator.pipeline_generator import PipelineGeneratorConfig, PipelineGenerator, read_test_steps, write_buildkite_steps
from buildkite.pipeline_generator.step import BuildkiteStep, BuildkiteBlockStep, DEFAULT_TEST_WORKING_DIR
from buildkite.pipeline_generator.utils import AgentQueue

TEST_COMMIT = "abcdef0123456789abcdef0123456789abcdef01"
TEST_CONTAINER_REGISTRY = "container.registry"
TEST_CONTAINER_REGISTRY_REPO = "test"


def _get_pipeline_generator_config():
    return PipelineGeneratorConfig(
        container_registry=TEST_CONTAINER_REGISTRY,
        container_registry_repo=TEST_CONTAINER_REGISTRY_REPO,
        commit=TEST_COMMIT,
        list_file_diff=[],
    )


def test_pipeline_generator_config_get_container_image():
    config = _get_pipeline_generator_config()
    config.validate()
    assert config.container_image == "container.registry/test:abcdef0123456789abcdef0123456789abcdef01"


@pytest.mark.parametrize(
    "commit",
    [
        "abcdefghijklmnopqrstuvwxyz1234567890abcd", # Invalid, not in a-f 0-9
        "1234567890abcdef", # Invalid, not 40 characters
    ]
)
def test_get_pipeline_generator_config_invalid_commit(commit):
    config = _get_pipeline_generator_config()
    config.commit = commit
    with pytest.raises(ValueError, match="not a valid Git commit hash"):
        config.validate()


def test_read_test_steps():
    current_dir = os.path.dirname(os.path.abspath(__file__))
    test_path = os.path.join(current_dir, "test_files/test-pipeline.yaml")
    test_steps = read_test_steps(test_path)
    assert len(test_steps) == 4
    assert test_steps[0].commands == ['echo "Test 1"']
    assert test_steps[0].command is None
    assert test_steps[0].working_dir == DEFAULT_TEST_WORKING_DIR

    assert test_steps[1].working_dir == "/tests2/"
    assert test_steps[1].no_gpu is True

    assert test_steps[2].commands == ['echo "Test 3"', 'echo "Test 3.1"']
    assert test_steps[2].source_file_dependencies == ["file1", "src/file2"]

    assert test_steps[3].commands == ['echo "Test 4.1"', 'echo "Test 4.2"']
    assert test_steps[3].num_nodes == 2
    assert test_steps[3].num_gpus == 4


def test_write_buildkite_steps():
    current_dir = os.path.dirname(os.path.abspath(__file__))
    expected_output_path = os.path.join(current_dir, "test_files/expected_pipeline.yaml")
    with open(expected_output_path, "r") as f:
        expected_output = yaml.safe_load(f)

    steps = [
        BuildkiteStep(label="Test 1", commands=['echo "Test1.1"', 'echo "Test1.2"']),
        BuildkiteStep(label="Test 2", commands=["command3"], agents = {"queue": AgentQueue.AWS_1xL4.value}),
        BuildkiteBlockStep(block="Run Test 3", key="block-test-3"),
        BuildkiteStep(label="Test 3", commands=["command4"], depends_on="block-test-3"),
    ]
    with tempfile.TemporaryDirectory() as temp_dir:
        output_file_path = os.path.join(temp_dir, "output.yaml")
        write_buildkite_steps(steps, output_file_path)
        with open(output_file_path, "r") as f:
            content = f.read()
        with open(expected_output_path, "r") as f:
            expected_content = f.read()
        assert content == expected_content
        

if __name__ == "__main__":
    sys.exit(pytest.main(["-v", __file__]))
