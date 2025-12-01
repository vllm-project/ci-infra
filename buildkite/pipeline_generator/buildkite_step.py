from pydantic import BaseModel
from typing import Dict, List, Optional, Any
from step import Step
from utils import get_agent_queue, get_image
from global_config import get_global_config
from plugin.k8s_plugin import get_k8s_plugin
from plugin.docker_plugin import get_docker_plugin
from utils import GPUType, get_list_file_diff

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
            "retry": self.retry
        }

class BuildkiteGroupStep(BaseModel):
    group: str
    steps: List[BuildkiteCommandStep]

def get_step_plugin(step: Step):
    # Use K8s plugin
    if step.gpu in [GPUType.H100.value, GPUType.A100.value]:
        return {"kubernetes": get_k8s_plugin(step, get_image())}
    else:
        return {"docker#v5.2.0": get_docker_plugin(step, get_image())}

def convert_group_step_to_buildkite_step(group_steps: Dict[str, List[Step]]) -> List[BuildkiteGroupStep]:
    buildkite_group_steps = []
    # inject values to replace variables in step commands
    global_config = get_global_config()
    variables_to_inject = {
        "$REGISTRY": global_config["registries"],
        "$REPO": ["main"] if global_config["branch"] == "main" else global_config["repositories"]["premerge"],
        "$BUILDKITE_COMMIT": global_config["commit"]
    }
    list_file_diff = get_list_file_diff()
    print(list_file_diff)
    for group, steps in group_steps.items():
        group_steps = []
        for step in steps:
            # check whether step should run automatically

            step_commands = step.commands
            for i, command in enumerate(step_commands):
                for variable, value in variables_to_inject.items():
                    step_commands[i] = step_commands[i].replace(variable, value)
            step.commands = step_commands
            buildkite_step = BuildkiteCommandStep(
                label=step.label,
                commands=step_commands,
                depends_on=step.depends_on,
                soft_fail=step.soft_fail,
                agents={"queue": get_agent_queue(step)},
            )
            if step.env:
                buildkite_step.env = step.env
            if step.retry:
                buildkite_step.retry = step.retry
            if step.key:
                buildkite_step.key = step.key
            # if step is image build, don't use docker plugin
            if not step.label.startswith(":docker:"):
                buildkite_step.plugins = [get_step_plugin(step)]
            group_steps.append(buildkite_step)
        buildkite_group_steps.append(BuildkiteGroupStep(group=group, steps=group_steps))
    return buildkite_group_steps
