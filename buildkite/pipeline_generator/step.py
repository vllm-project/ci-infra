from pydantic import BaseModel
from typing import Optional, List, Dict, Any
from pydantic import model_validator
from typing_extensions import Self
from collections import defaultdict
from global_config import get_global_config
import os
import yaml


class Step(BaseModel):
    label: str
    group: str = ""
    working_dir: Optional[str] = None
    key: Optional[str] = None
    depends_on: Optional[List[str]] = None
    commands: Optional[List[str]] = None
    device: Optional[str] = None
    num_devices: Optional[int] = None
    num_nodes: Optional[int] = None
    source_file_dependencies: Optional[List[str]] = None
    soft_fail: Optional[bool] = False
    parallelism: Optional[int] = None
    mount_buildkite_agent: Optional[bool] = False
    env: Optional[Dict[str, str]] = None
    retry: Optional[Dict[str, Any]] = None
    optional: Optional[bool] = False
    no_plugin: Optional[bool] = False
    mirror: Optional[Dict[str, Dict[str, Any]]] = None

    @model_validator(mode="after")
    def validate_multi_node(self) -> Self:
        if self.num_nodes and not self.num_devices:
            raise ValueError("'num_devices' must be defined if 'num_nodes' is defined.")
        return self

    @classmethod
    def from_yaml(cls, yaml_data: dict):
        return cls(**yaml_data)


def parse_steps_from_yaml(yaml_data: dict):
    group = yaml_data.get("group", None)
    yaml_steps = yaml_data.get("steps", [])
    steps = [Step.from_yaml(step) for step in yaml_steps]
    if group:
        for step in steps:
            step.group = group
    return steps


def read_steps_from_job_dir(job_dir: str):
    global_config = get_global_config()
    steps = []
    for root, _, files in os.walk(job_dir):
        for file in files:
            if not file.endswith(".yaml"):
                continue
            yaml_path = os.path.join(root, file)
            with open(yaml_path, "r") as f:
                data = yaml.safe_load(f)
            group_depends_on = data.get("depends_on")
            file_steps = parse_steps_from_yaml(data)
            if group_depends_on:
                for step in file_steps:
                    if not step.depends_on:
                        step.depends_on = group_depends_on
                    if (
                        not step.working_dir
                        and global_config["github_repo_name"] == "vllm-project/vllm"
                    ):
                        step.working_dir = "/vllm-workspace/tests"
                    step.source_file_dependencies = getattr(step, "source_file_dependencies", [])
                    if not step.source_file_dependencies:
                        step.source_file_dependencies = []
                    step.source_file_dependencies.append(os.path.relpath(yaml_path))
            steps.extend(file_steps)
    return steps


def group_steps(steps: List[Step]) -> Dict[str, List[Step]]:
    grouped_steps = defaultdict(list)
    for step in steps:
        if step.group:
            grouped_steps[step.group].append(step)
        else:
            grouped_steps["ungrouped"].append(step)
    sorted_grouped_steps = {}
    for group, steps in grouped_steps.items():
        sorted_grouped_steps[group] = sorted(steps, key=lambda x: x.label)
    return sorted_grouped_steps
