import os
from collections import defaultdict
from typing import Any, Dict, List, Optional

import yaml
from global_config import get_global_config
from pydantic import BaseModel, Field, model_validator
from typing_extensions import Self


class Step(BaseModel):
    label: str
    group: str = ""
    working_dir: Optional[str] = None
    key: Optional[str] = None
    depends_on: Optional[List[str]] = None
    commands: Optional[List[str]] = None
    device: Optional[str] = None
    agent_tags: Optional[Dict[str, str]] = None
    num_devices: Optional[int] = None
    num_nodes: Optional[int] = None
    source_file_dependencies: Optional[List[str]] = None
    soft_fail: Optional[bool] = False
    parallelism: Optional[int] = None
    concurrency: Optional[int] = Field(default=None, gt=0, strict=True)
    concurrency_group: Optional[str] = None
    timeout_in_minutes: Optional[int] = None
    mount_buildkite_agent: Optional[bool] = False
    env: Optional[Dict[str, str]] = None
    retry: Optional[Dict[str, Any]] = None
    optional: Optional[bool] = False
    no_plugin: Optional[bool] = False
    no_gpu: Optional[bool] = False
    dind: bool = True
    mirror: Optional[Dict[str, Dict[str, Any]]] = None
    trace_represented_job_key: Optional[str] = None
    trace_gpu: bool = False
    trace_collector_sha256: Optional[str] = None
    trace_subprocess_coverage: bool = False
    trace_capture_class: str | None = None

    def otel_tracing_enabled(self) -> bool:
        config = get_global_config()
        treatment_branch = os.getenv("CI_INFRA_OTEL_TREATMENT_BRANCH", "")
        trusted_branch = config["branch"] == "main" or bool(
            treatment_branch
            and treatment_branch == config["branch"]
            and os.getenv("BUILDKITE_SOURCE") == "api"
        )
        return bool(
            config["github_repo_name"] == "vllm-project/vllm"
            and trusted_branch
            and config["pull_request"] == "false"
        )

    @model_validator(mode="after")
    def validate_multi_node(self) -> Self:
        if self.num_nodes and not self.num_devices:
            raise ValueError("'num_devices' must be defined if 'num_nodes' is defined.")
        if (self.concurrency is None) != (self.concurrency_group is None):
            raise ValueError(
                "'concurrency' and 'concurrency_group' must be defined together."
            )
        if self.concurrency_group is not None and not self.concurrency_group.strip():
            raise ValueError("'concurrency_group' must be a nonempty string.")
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
                    step.source_file_dependencies = getattr(
                        step, "source_file_dependencies", []
                    )
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
