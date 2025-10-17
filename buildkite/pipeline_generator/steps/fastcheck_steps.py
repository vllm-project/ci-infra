"""Fastcheck-specific step conversion logic."""
from typing import List

from ..models.buildkite_step import BuildkiteStep
from ..utils.agents import get_agent_queue
from ..utils.constants import AgentQueue
from ..docker.fastcheck_plugin_builder import build_fastcheck_docker_plugin
from ..shared.job_labels import TestLabels


def convert_fastcheck_multi_node_test_step(test_step, container_image: str, config) -> BuildkiteStep:
    """Convert a multi-node test step for fastcheck mode."""
    working_dir = test_step.working_dir or "/vllm-workspace/tests"
    
    # Extract commands for each node
    node_commands: List[List[str]] = []
    if test_step.commands and len(test_step.commands) > 0 and isinstance(test_step.commands[0], list):
        node_commands = test_step.commands  # type: ignore
    else:
        simple_commands: List[str] = test_step.commands if test_step.commands else []  # type: ignore
        node_commands = [simple_commands] * (test_step.num_nodes or 2)
    
    # Build the multi-node command
    quoted_node_commands = []
    for node_cmds in node_commands:
        node_cmd_str = " && ".join(node_cmds)
        quoted_node_commands.append(f'"{node_cmd_str}"')
    
    multi_node_cmd = (
        f"./.buildkite/scripts/run-multi-node-test.sh "
        f"{working_dir} {test_step.num_nodes} {test_step.num_gpus or 1} "
        f"{container_image} {' '.join(quoted_node_commands)}"
    )
    
    agent_queue = get_agent_queue(test_step.no_gpu, test_step.gpu, test_step.num_gpus, test_step.label)
    
    # Fastcheck multi-node tests have NO retry, NO soft_fail, NO plugins
    return BuildkiteStep(
        label=test_step.label,
        key=None,
        commands=[multi_node_cmd],
        parallelism=test_step.parallelism,
        soft_fail=None,  # No soft_fail for multi-node in fastcheck
        plugins=None,
        agents={"queue": agent_queue.value},
        timeout_in_minutes=None,
        depends_on="build",
        retry=None  # No retry for multi-node in fastcheck
    )


def convert_fastcheck_test_step(test_step, container_image: str, config) -> BuildkiteStep:
    """Convert TestStep to BuildkiteStep specifically for fastcheck mode."""
    
    # Check if this is a multi-node test
    if test_step.num_nodes and test_step.num_nodes >= 2:
        return convert_fastcheck_multi_node_test_step(test_step, container_image, config)
    
    # A100 uses Kubernetes in fastcheck with special config
    from ..utils.constants import GPUType
    from ..docker.fastcheck_plugin_builder import build_fastcheck_a100_kubernetes_plugin
    if test_step.gpu == GPUType.A100:
        plugin_config = build_fastcheck_a100_kubernetes_plugin(test_step, container_image, config)
        
        return BuildkiteStep(
            label=test_step.label,
            key=None,
            commands=[],
            parallelism=test_step.parallelism,
            soft_fail=test_step.soft_fail or False,
            plugins=[plugin_config],
            agents={"queue": AgentQueue.A100.value},
            timeout_in_minutes=None,
            depends_on=None,  # A100 in fastcheck has no depends_on
            retry={
                "automatic": [
                    {"exit_status": -1, "limit": 5},
                    {"exit_status": -10, "limit": 5}
                ]
            },
            priority=10000  # A100 tests have priority
        )
    
    # Get fastcheck-specific plugin configuration
    plugin_config = build_fastcheck_docker_plugin(test_step, container_image, config)
    
    # Determine agents - fastcheck uses simple queues
    if test_step.label == TestLabels.DOCUMENTATION_BUILD:
        agent_queue = AgentQueue.SMALL_CPU_PREMERGE
    elif test_step.no_gpu:
        agent_queue = AgentQueue.AWS_CPU_PREMERGE_SIMPLE  # Fastcheck uses simple queue
    elif test_step.num_gpus == 2 or test_step.num_gpus == 4:
        agent_queue = AgentQueue.AWS_4xL4
    else:
        agent_queue = AgentQueue.AWS_1xL4
    
    # Build the Buildkite step with fastcheck-specific settings
    buildkite_step = BuildkiteStep(
        label=test_step.label,
        key=None,
        commands=[],
        parallelism=test_step.parallelism,
        soft_fail=test_step.soft_fail or False,  # Fastcheck template sets False as default
        plugins=[plugin_config],
        agents={"queue": agent_queue.value},
        timeout_in_minutes=None,
        depends_on="build",
        retry={
            "automatic": [
                {"exit_status": -1, "limit": 5},
                {"exit_status": -10, "limit": 5}
            ]
        }
    )
    
    return buildkite_step

