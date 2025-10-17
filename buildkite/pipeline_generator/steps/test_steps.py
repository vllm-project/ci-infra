"""Test step conversion logic."""
from typing import List

from ..models.buildkite_step import BuildkiteStep
from ..utils.agents import get_agent_queue
from ..docker.plugin_builder import build_plugin_for_test_step


def convert_multi_node_test_step(test_step, container_image: str, config) -> BuildkiteStep:
    """Convert a multi-node test step to Buildkite format."""
    working_dir = test_step.working_dir or "/vllm-workspace/tests"
    
    # Extract commands for each node
    node_commands: List[List[str]] = []
    if test_step.commands and len(test_step.commands) > 0 and isinstance(test_step.commands[0], list):
        # Multi-node format: list of lists
        node_commands = test_step.commands  # type: ignore
    else:
        # Fallback: use the same commands for all nodes
        simple_commands: List[str] = test_step.commands if test_step.commands else []  # type: ignore
        node_commands = [simple_commands] * (test_step.num_nodes or 2)
    
    # Build the multi-node command
    quoted_node_commands = []
    for node_cmds in node_commands:
        # Join commands for this node
        node_cmd_str = " && ".join(node_cmds)
        # Use double quotes to match jinja template
        quoted_node_commands.append(f'"{node_cmd_str}"')
    
    multi_node_cmd = (
        f"./.buildkite/scripts/run-multi-node-test.sh "
        f"{working_dir} {test_step.num_nodes} {test_step.num_gpus or 1} "
        f"{container_image} {' '.join(quoted_node_commands)}"
    )
    
    agent_queue = get_agent_queue(test_step.no_gpu, test_step.gpu, test_step.num_gpus, test_step.label)
    
    # Determine retry limits based on pipeline mode
    from ..utils.constants import PipelineMode
    retry_limit = 5 if config.pipeline_mode == PipelineMode.FASTCHECK else 1
    
    return BuildkiteStep(
        label=test_step.label,
        key=None,  # Don't add keys to regular test steps (matches jinja)
        commands=[multi_node_cmd],
        parallelism=test_step.parallelism,
        soft_fail=test_step.soft_fail or False,
        plugins=None,  # No docker plugin for multi-node
        agents={"queue": agent_queue.value},
        timeout_in_minutes=None,  # Multi-node steps don't include timeout in plugins
        depends_on="build",  # Test steps depend on build  
        retry={
            "automatic": [
                {"exit_status": -1, "limit": retry_limit},
                {"exit_status": -10, "limit": retry_limit}
            ]
        }
    )


def convert_test_step_to_buildkite_step(test_step, container_image: str, config) -> BuildkiteStep:
    """Convert TestStep into BuildkiteStep using data-driven configuration."""
    
    # Check if this is a multi-node test
    if test_step.num_nodes and test_step.num_nodes >= 2:
        return convert_multi_node_test_step(test_step, container_image, config)
    
    # Get plugin configuration using docker plugin builder
    plugin_config = build_plugin_for_test_step(test_step, container_image, config)
    
    # Determine agents
    agent_queue = get_agent_queue(test_step.no_gpu, test_step.gpu, test_step.num_gpus, test_step.label)
    
    # Determine retry limits based on pipeline mode
    from ..utils.constants import PipelineMode
    retry_limit = 5 if config.pipeline_mode == PipelineMode.FASTCHECK else 1
    
    # Build the Buildkite step
    # Note: Jinja template doesn't add keys or empty fields to regular test steps
    buildkite_step = BuildkiteStep(
        label=test_step.label,
        key=None,  # Don't add keys to regular test steps (matches jinja)
        commands=[],  # Empty for docker plugin steps (command is in plugin)
        parallelism=test_step.parallelism,
        soft_fail=test_step.soft_fail or False,
        plugins=[plugin_config],
        agents={"queue": agent_queue.value},
        timeout_in_minutes=None,  # Don't include timeout unless jinja does
        depends_on="build",  # Will be overridden by caller
        retry={
            "automatic": [
                {"exit_status": -1, "limit": retry_limit},
                {"exit_status": -10, "limit": retry_limit}
            ]
        }
    )
    
    return buildkite_step

