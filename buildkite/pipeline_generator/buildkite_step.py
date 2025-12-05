from pydantic import BaseModel
from typing import Dict, List, Optional, Any, Union, TypedDict
import copy
from step import Step
from utils import get_agent_queue, get_image
from global_config import get_global_config
from plugin.k8s_plugin import get_k8s_plugin
from plugin.docker_plugin import get_docker_plugin
from utils import GPUType

class BuildkiteCommandStep(BaseModel):
    label: str
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
            "retry": self.retry,
            "parallelism": self.parallelism
        }

class BuildkiteBlockStep(BaseModel):
    block: str
    depends_on: Optional[Union[str, List[str]]] = None
    key: Optional[str] = None

    def to_yaml(self):
        return {
            "block": self.block,
            "depends_on": self.depends_on,
            "key": self.key
        }

class BuildkiteGroupStep(BaseModel):
    group: str
    steps: List[Union[BuildkiteCommandStep, BuildkiteBlockStep]]

def get_step_plugin(step: Step):
    # Use K8s plugin
    if step.gpu in [GPUType.H100.value, GPUType.A100.value]:
        return get_k8s_plugin(step, get_image(step.no_gpu))
    else:
        return {"docker#v5.2.0": get_docker_plugin(step, get_image(step.no_gpu))}

def convert_group_step_to_buildkite_step(group_steps: Dict[str, List[Step]]) -> List[BuildkiteGroupStep]:
    buildkite_group_steps = []
    # inject values to replace variables in step commands
    global_config = get_global_config()
    if global_config["name"] == "vllm_ci":
        cache_from_tag, cache_to_tag = get_ecr_cache_registry()
        variables_to_inject = {
            "$REGISTRY": global_config["registries"],
            "$REPO": ["main"] if global_config["branch"] == "main" else global_config["repositories"]["premerge"],
            "$BUILDKITE_COMMIT": "$$BUILDKITE_COMMIT",
            "$BRANCH": global_config["branch"],
            "$VLLM_USE_PRECOMPILED": "1" if global_config["use_precompiled"] else "0",
            "$VLLM_MERGE_BASE_COMMIT": global_config["merge_base_commit"],
            "$CACHE_FROM": cache_from_tag,
            "$CACHE_TO": cache_to_tag,
        }
    else:
        variables_to_inject = {}
    list_file_diff = global_config["list_file_diff"]
    for group, steps in group_steps.items():
        group_steps = []
        for step in steps:
            # generate block step if step should not run automatically
            block_step = None
            if 1==1 or not step_should_run(step, list_file_diff):
                block_step = BuildkiteBlockStep(
                    block=f"Run {step.label}",
                    depends_on=[],
                    key=f"block-{generate_step_key(step.label)}"
                )
                if step.label.startswith(":docker:"):
                    block_step.depends_on = []
                group_steps.append(block_step)
            step_commands = step.commands
            for i, command in enumerate(step_commands):
                for variable, value in variables_to_inject.items():
                    step_commands[i] = step_commands[i].replace(variable, value)
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
            # if step is image build / multi-node test, don't use docker plugin
            if step.label.startswith(":docker:") or (step.num_nodes and step.num_nodes >= 2):
                pass
            else:
                buildkite_step.plugins = [get_step_plugin(step)]
                buildkite_step.commands = [f"cd {step.working_dir}", *buildkite_step.commands]
            group_steps.append(buildkite_step)
        buildkite_group_steps.append(BuildkiteGroupStep(group=group, steps=group_steps))
    return buildkite_group_steps

def step_should_run(step: Step, list_file_diff: List[str]) -> bool:
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
    return True

def generate_step_key(step_label: str) -> str:
    return (
        step_label
        .replace(" ", "-")
        .lower()
        .replace("(", "")
        .replace(")", "")
        .replace("%", "")
        .replace(",", "-")
        .replace("+", "-")
        .replace(":", "-")
        .replace(".", "-")
    )
