from pydantic import BaseModel
from typing import Dict, List, Optional, Any, Union
from step import Step
from utils_lib.docker_utils import get_image, get_ecr_cache_registry
from global_config import get_global_config
from plugin.k8s_plugin import get_k8s_plugin
from plugin.docker_plugin import get_docker_plugin
from constants import GPUType, AgentQueue


class BuildkiteCommandStep(BaseModel):
    label: str
    group: Optional[str] = None
    key: Optional[str] = None
    agents: Dict[str, str] = {}
    commands: List[str] = []
    depends_on: Optional[List[str]] = None
    soft_fail: Optional[bool] = False
    retry: Optional[Dict[str, Any]] = None
    plugins: Optional[List[Dict[str, Any]]] = None
    env: Optional[Dict[str, str]] = None
    parallelism: Optional[int] = None

    def to_yaml(self):
        return {
            "label": self.label,
            "group": self.group,
            "commands": self.commands,
            "depends_on": self.depends_on,
            "soft_fail": self.soft_fail,
            "retry": self.retry,
            "plugins": self.plugins,
            "env": self.env,
            "parallelism": self.parallelism,
        }


class BuildkiteBlockStep(BaseModel):
    block: str
    depends_on: Optional[Union[str, List[str]]] = None
    key: Optional[str] = None

    def to_yaml(self):
        return {"block": self.block, "depends_on": self.depends_on, "key": self.key}


class BuildkiteGroupStep(BaseModel):
    group: str
    steps: List[Union[BuildkiteCommandStep, BuildkiteBlockStep]]


def _get_step_plugin(step: Step):
    # Use K8s plugin
    if step.gpu in [GPUType.H100.value, GPUType.A100.value]:
        return get_k8s_plugin(step, get_image(step.no_gpu))
    else:
        return {"docker#v5.2.0": get_docker_plugin(step, get_image(step.no_gpu))}


def get_agent_queue(step: Step):
    branch = get_global_config()["branch"]
    if step.label.startswith(":docker:"):
        if branch == "main":
            return AgentQueue.CPU_QUEUE_POSTMERGE_US_EAST_1
        else:
            return AgentQueue.CPU_QUEUE_PREMERGE_US_EAST_1
    elif step.label == "Documentation Build":
        return AgentQueue.SMALL_CPU_QUEUE_PREMERGE
    elif step.no_gpu:
        return AgentQueue.CPU_QUEUE_PREMERGE_US_EAST_1
    elif step.gpu == GPUType.A100:
        return AgentQueue.A100_QUEUE
    elif step.gpu == GPUType.H100:
        return AgentQueue.MITHRIL_H100_POOL
    elif step.gpu == GPUType.H200:
        return AgentQueue.SKYLAB_H200
    elif step.gpu == GPUType.B200:
        return AgentQueue.B200
    elif step.num_gpus == 2 or step.num_gpus == 4:
        return AgentQueue.GPU_4_QUEUE
    else:
        return AgentQueue.GPU_1_QUEUE


def _get_variables_to_inject() -> Dict[str, str]:
    global_config = get_global_config()
    if global_config["name"] != "vllm_ci":
        return {}

    cache_from_tag, cache_to_tag = get_ecr_cache_registry()
    return {
        "$REGISTRY": global_config["registries"],
        "$REPO": global_config["repositories"]["main"]
        if global_config["branch"] == "main"
        else global_config["repositories"]["premerge"],
        "$BUILDKITE_COMMIT": "$$BUILDKITE_COMMIT",
        "$BRANCH": global_config["branch"],
        "$VLLM_USE_PRECOMPILED": "1" if global_config["use_precompiled"] else "0",
        "$VLLM_MERGE_BASE_COMMIT": global_config["merge_base_commit"],
        "$CACHE_FROM": cache_from_tag,
        "$CACHE_TO": cache_to_tag,
    }


def _prepare_commands(step: Step, variables_to_inject: Dict[str, str]) -> List[str]:
    """Prepare step commands with variables injected and default setup commands."""
    commands = []
    # Default setup commands
    if not step.label.startswith(":docker:"):
        commands.append("(command nvidia-smi || true)")

    if step.commands:
        commands.extend(step.commands)

    final_commands = []
    for command in commands:
        if not step.num_nodes:
            command = command.replace("'", '"')
        for variable, value in variables_to_inject.items():
            command = command.replace(variable, value)
        final_commands.append(command)

    if step.working_dir and not (
        step.label.startswith(":docker:") or (step.num_nodes and step.num_nodes >= 2)
    ):
        final_commands.insert(0, f"cd {step.working_dir}")

    return final_commands


def _create_block_step(
    step: Step, list_file_diff: List[str]
) -> Optional[BuildkiteBlockStep]:
    if _step_should_run(step, list_file_diff):
        return None

    block_step = BuildkiteBlockStep(
        block=f"Run {step.label}",
        depends_on=[],
        key=f"block-{_generate_step_key(step.label)}",
    )
    if step.label.startswith(":docker:"):
        block_step.depends_on = []
    return block_step


def convert_group_step_to_buildkite_step(
    group_steps: Dict[str, List[Step]],
) -> List[BuildkiteGroupStep]:
    buildkite_group_steps = []
    variables_to_inject = _get_variables_to_inject()
    global_config = get_global_config()
    list_file_diff = global_config["list_file_diff"]

    for group, steps in group_steps.items():
        group_steps_list = []
        for step in steps:
            # block step
            block_step = _create_block_step(step, list_file_diff)
            if block_step:
                group_steps_list.append(block_step)

            # command step
            step_commands = _prepare_commands(step, variables_to_inject)

            buildkite_step = BuildkiteCommandStep(
                label=step.label,
                commands=step_commands,
                depends_on=step.depends_on,
                soft_fail=step.soft_fail,
                agents={"queue": get_agent_queue(step)},
            )

            if block_step:
                buildkite_step.depends_on = block_step.key
            if step.env:
                buildkite_step.env = step.env
            if step.retry:
                buildkite_step.retry = step.retry
            if step.key:
                buildkite_step.key = step.key
            if step.parallelism:
                buildkite_step.parallelism = step.parallelism

            # add plugin
            if not step.no_plugin and not (
                step.label.startswith(":docker:")
                or (step.num_nodes and step.num_nodes >= 2)
            ):
                buildkite_step.plugins = [_get_step_plugin(step)]

            group_steps_list.append(buildkite_step)

        buildkite_group_steps.append(
            BuildkiteGroupStep(group=group, steps=group_steps_list)
        )

    return buildkite_group_steps


def _step_should_run(step: Step, list_file_diff: List[str]) -> bool:
    global_config = get_global_config()
    if global_config["nightly"] == "1":
        return True
    if step.optional:
        return False
    if global_config["run_all"]:
        return True
    if step.source_file_dependencies:
        for source_file in step.source_file_dependencies:
            for diff_file in list_file_diff:
                if source_file in diff_file:
                    return True
    return False


def _generate_step_key(step_label: str) -> str:
    return (
        step_label.replace(" ", "-")
        .lower()
        .replace("(", "")
        .replace(")", "")
        .replace("%", "")
        .replace(",", "-")
        .replace("+", "-")
        .replace(":", "-")
        .replace(".", "-")
    )
