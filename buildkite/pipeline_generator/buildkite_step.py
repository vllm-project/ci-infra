from pydantic import BaseModel
from typing import Dict, List, Optional, Any
from step import Step
from pipeline_generator_helper import get_agent_queue
from plugin.k8s_plugin import get_k8s_plugin
from plugin.docker_plugin import get_docker_plugin
from utils import GPUType

class BuildkiteCommandStep(BaseModel):
    label: str
    group: str
    agents: Dict[str, str] = {}
    commands: List[str] = []
    depends_on: Optional[List[str]] = None
    soft_fail: Optional[bool] = False
    retry: Optional[Dict[str, Any]] = None
    plugins: Optional[List[Dict[str, Any]]] = None

    def to_yaml(self):
        return {
            "label": self.label,
            "group": self.group,
            "commands": self.commands,
            "depends_on": self.depends_on,
            "soft_fail": self.soft_fail,
            "retry": self.retry,
            "plugins": self.plugins
        }

def get_step_plugin(step: Step, image: str):
    # Use K8s plugin
    if step.gpu in [GPUType.H100, GPUType.H200]:
        return get_k8s_plugin(step, image)
    else:
        return get_docker_plugin(step, image)

def convert_step_to_buildkite_step(step: Step, image: str):
    buildkite_step = BuildkiteCommandStep(
        label=step.label,
        group=step.group,
        commands=step.commands,
        depends_on=step.depends_on,
        soft_fail=step.soft_fail,
        agents={"queue": get_agent_queue(step)},
        plugins=[get_step_plugin(step, image)]
    )
    return buildkite_step
