import pytest
import sys
from unittest import mock

from buildkite.pipeline_generator.pipeline_generator_helper import get_plugin_config, convert_test_step_to_buildkite_step
from buildkite.pipeline_generator.utils import GPUType
from buildkite.pipeline_generator.step import TestStep, BuildkiteStep

@mock.patch("buildkite.pipeline_generator.pipeline_generator_helper.get_kubernetes_plugin_config")
@mock.patch("buildkite.pipeline_generator.pipeline_generator_helper.get_docker_plugin_config")
def test_get_plugin_config(mock_get_docker_plugin_config, mock_get_kubernetes_plugin_config):
    mock_get_docker_plugin_config.return_value = {"docker": "plugin"}
    mock_get_kubernetes_plugin_config.return_value = {"kubernetes": "plugin"}
    plugin = get_plugin_config("image:latest")
    assert plugin == {"docker": "plugin"}
    assert mock_get_docker_plugin_config.call_count == 1
    assert mock_get_docker_plugin_config.call_args[0] == ("image:latest", False)

@mock.patch("buildkite.pipeline_generator.pipeline_generator_helper.get_kubernetes_plugin_config")
@mock.patch("buildkite.pipeline_generator.pipeline_generator_helper.get_docker_plugin_config")
def test_get_plugin_config_kubernetes(mock_get_docker_plugin_config, mock_get_kubernetes_plugin_config):
    mock_get_docker_plugin_config.return_value = {"docker": "plugin"}
    mock_get_kubernetes_plugin_config.return_value = {"kubernetes": "plugin"}
    plugin = get_plugin_config(container_image="image:latest", gpu_type=GPUType.A100, num_gpus=4)
    assert plugin == {"kubernetes": "plugin"}
    assert mock_get_kubernetes_plugin_config.call_count == 1
    assert mock_get_kubernetes_plugin_config.call_args[0] == ("image:latest", 4)

@pytest.mark.parametrize(
    ("test_step", "expected_buildkite_step"),
    [
        # Regular test with plugin
        (
            TestStep(
                label="First test",
                commands=["echo A", "echo B", "echo C"],
                num_gpus=1,
            ),
            BuildkiteStep(
                label="First test",
                key="first-test",
                commands=["echo A", "echo B", "echo C"],
                plugins=[{"plugin": "config"}],
                agents={"queue": "gpu_1_queue"}
            )
        ),
        # Multi node test without plugin and custom command for multi-node
        (
            TestStep(
                label="Second test",
                commands=["echo D", "echo E"],
                num_nodes=2,
                num_gpus=2,
            ),
            BuildkiteStep(
                label="Second test",
                key="second-test",
                commands=["multi-node-command"],
                plugins=None,
                agents={"queue": "gpu_4_queue"}
            )
        )
    ]
)
@mock.patch("buildkite.pipeline_generator.pipeline_generator_helper.get_multi_node_test_command")
@mock.patch("buildkite.pipeline_generator.pipeline_generator_helper.get_plugin_config")
def test_convert_test_step(mock_get_plugin_config, mock_get_multi_node_test_command, test_step, expected_buildkite_step):
    mock_get_plugin_config.return_value = {"plugin": "config"}
    mock_get_multi_node_test_command.return_value = "multi-node-command"

    buildkite_step = convert_test_step_to_buildkite_step(test_step, "image:latest")
    assert buildkite_step == expected_buildkite_step


if __name__ == "__main__":
    sys.exit(pytest.main(["-v", __file__]))
