"""Fastcheck-specific step conversion logic."""

from typing import List

from ..data_models.buildkite_step import BuildkiteStep
from ..data_models.test_step import TestStep
from ..pipeline_config import PipelineGeneratorConfig
from ..utils.agent_queues import get_agent_queue
from ..utils.constants import (
    DEFAULT_WORKING_DIR,
    AgentQueue,
    GPUType,
    PriorityValues,
    RetryConfig,
    Scripts,
    TestLabels,
)
from .docker_plugins import (
    build_fastcheck_a100_kubernetes_plugin,
    build_fastcheck_docker_plugin,
)


def convert_fastcheck_multi_node_test_step(test_step: TestStep, container_image: str, config: PipelineGeneratorConfig) -> BuildkiteStep:
    """Convert a multi-node test step for fastcheck mode."""
    working_dir = test_step.working_dir or DEFAULT_WORKING_DIR

    # Extract commands for each node
    node_commands: List[List[str]] = []
    if test_step.commands and len(test_step.commands) > 0 and isinstance(test_step.commands[0], list):
        node_commands = test_step.commands  # type: ignore
    else:
        # type: ignore
        simple_commands: List[str] = test_step.commands if test_step.commands else []
        node_commands = [simple_commands] * (test_step.num_nodes or 2)

    # Build the multi-node command
    quoted_node_commands = []
    for node_cmds in node_commands:
        node_cmd_str = " && ".join(node_cmds)
        quoted_node_commands.append(f'"{node_cmd_str}"')

    multi_node_cmd = (
        f"{Scripts.RUN_MULTI_NODE_TEST} {working_dir} {test_step.num_nodes} {test_step.num_gpus or 1} {container_image} {' '.join(quoted_node_commands)}"
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
        agents={"queue": agent_queue},
        timeout_in_minutes=None,
        depends_on="build",
        retry=None,  # No retry for multi-node in fastcheck
    )


def convert_fastcheck_test_step(test_step: TestStep, container_image: str, config: PipelineGeneratorConfig) -> BuildkiteStep:
    """Convert TestStep to BuildkiteStep specifically for fastcheck mode."""

    # Check if this is a multi-node test
    if test_step.num_nodes and test_step.num_nodes >= 2:
        return convert_fastcheck_multi_node_test_step(test_step, container_image, config)

    # A100 uses Kubernetes in fastcheck with special config
    if test_step.gpu == GPUType.A100:
        plugin_config = build_fastcheck_a100_kubernetes_plugin(test_step, container_image)

        return BuildkiteStep(
            label=test_step.label,
            key=None,
            commands=[],
            parallelism=test_step.parallelism,
            soft_fail=test_step.soft_fail or False,
            plugins=[plugin_config],
            agents={"queue": AgentQueue.A100_QUEUE},
            timeout_in_minutes=None,
            depends_on=None,  # A100 in fastcheck has no depends_on
            retry={
                "automatic": [
                    {"exit_status": RetryConfig.EXIT_STATUS_AGENT_LOST, "limit": 5},
                    {"exit_status": RetryConfig.EXIT_STATUS_AGENT_TERMINATED, "limit": 5},
                ]
            },
            priority=PriorityValues.A100_TESTS,
        )

    # Get fastcheck-specific plugin configuration
    plugin_config = build_fastcheck_docker_plugin(test_step, container_image, config)

    # Determine agents - fastcheck uses simple queues
    if test_step.label == TestLabels.DOCUMENTATION_BUILD:
        agent_queue = AgentQueue.SMALL_CPU_QUEUE_PREMERGE
    elif test_step.no_gpu:
        agent_queue = AgentQueue.CPU_QUEUE_PREMERGE  # Fastcheck uses simple queue
    elif test_step.num_gpus == 2 or test_step.num_gpus == 4:
        agent_queue = AgentQueue.GPU_4_QUEUE
    else:
        agent_queue = AgentQueue.GPU_1_QUEUE

    # Build the Buildkite step with fastcheck-specific settings
    buildkite_step = BuildkiteStep(
        label=test_step.label,
        key=None,
        commands=[],
        parallelism=test_step.parallelism,
        soft_fail=test_step.soft_fail or False,
        # Fastcheck template sets False as default
        plugins=[plugin_config],
        agents={"queue": agent_queue},
        timeout_in_minutes=None,
        depends_on="build",
        retry={
            "automatic": [
                {"exit_status": RetryConfig.EXIT_STATUS_AGENT_LOST, "limit": 5},
                {"exit_status": RetryConfig.EXIT_STATUS_AGENT_TERMINATED, "limit": 5},
            ]
        },
    )

    return buildkite_step
