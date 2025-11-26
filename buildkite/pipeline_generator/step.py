from pydantic import BaseModel
from typing import Optional, List, Dict
from pydantic import model_validator
from typing_extensions import Self
from collections import defaultdict

import os
import yaml

DEFAULT_TEST_WORKING_DIR = "/vllm-workspace/tests"

class Step(BaseModel):
    label: str
    group: str = ""
    working_dir: str = DEFAULT_TEST_WORKING_DIR
    depends_on: Optional[List[str]] = None
    commands: Optional[List[str]] = None
    gpu: Optional[str] = None
    no_gpu: Optional[bool] = False
    num_gpus: Optional[int] = None
    num_nodes: Optional[int] = None
    source_file_dependencies: Optional[List[str]] = None
    soft_fail: Optional[bool] = False
    parallelism: Optional[int] = None
    mount_buildkite_agent: Optional[bool] = False

    @model_validator(mode="after")
    def validate_gpu(self) -> Self:
        if self.gpu and self.no_gpu:
            raise ValueError("Both 'gpu' and 'no_gpu' cannot be defined together.")
        return self

    @model_validator(mode="after")
    def validate_multi_node(self) -> Self:
        if self.num_nodes and not self.num_gpus:
            raise ValueError("'num_gpus' must be defined if 'num_nodes' is defined.")
        return self
    
    @classmethod
    def from_yaml(cls, yaml_data: dict):
        return cls(**yaml_data)

def read_steps_from_job_dir(job_dir: str):
    steps = []
    for root, _, files in os.walk(job_dir):
        for file in files:
            if file.endswith(".yaml"):
                with open(os.path.join(root, file), "r") as f:
                    data = yaml.safe_load(f)
                    steps.extend(parse_steps_from_yaml(data))
    return steps

def parse_steps_from_yaml(yaml_data: dict):
    group = yaml_data.get("group", None)
    yaml_steps = yaml_data.get("steps", [])
    steps = [Step.from_yaml(step) for step in yaml_steps]
    if group:
        for step in steps:
            step.group = group
    return steps

def group_steps(steps: List[Step]) -> Dict[str, List[Step]]:
    grouped_steps = defaultdict(list)
    for step in steps:
        if step.group:
            grouped_steps[step.group].append(step)
        else:
            grouped_steps["ungrouped"].append(step)
    return grouped_steps

def group_and_sort_steps(steps: List[Step]) -> Dict[str, List[Step]]:
    # Sort steps by group and label
    grouped_steps = group_steps(steps)
    sorted_group_steps = {}
    for group, steps in grouped_steps.items():
        sorted_group_steps[group] = sorted(steps, key=lambda x: x.label)
    return sorted_group_steps
