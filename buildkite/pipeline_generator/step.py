from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from pydantic import PrivateAttr, model_validator
from typing_extensions import Self
from collections import defaultdict
from global_config import get_global_config
import os
import yaml

from step_vendors import (
    EXACT_MIRROR,
    MIRROR_HARDWARE,
    DeviceStepError,
    check_block_style,
    check_written_step,
    compile_device_step,
    is_device_step,
)


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
    # Plan this step's tests into shards from main's timings (runtime_shard.py).
    # Unset follows the pipeline config's automatic_shard_default.
    automatic_shard: Optional[bool] = None
    # Set by read_steps_from_job_dir: the step's yaml file, and whether the step
    # lists that file itself.
    source_file: Optional[str] = Field(default=None, exclude=True)
    source_file_declared: bool = Field(default=False, exclude=True)
    # The same for a `devices:` step's AMD mirror, which lists its own.
    mirror_source_file_declared: bool = Field(default=False, exclude=True)
    # Whether a `devices:` step compiled to this one (check_device_step_keys).
    _from_devices: bool = PrivateAttr(default=False)
    # The index of the yaml step it comes from, in its file.
    _origin: int = PrivateAttr(default=-1)

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
    steps = []
    for origin, raw in enumerate(yaml_steps):
        if is_device_step(raw):
            for compiled in compile_device_step(raw, yaml_data.get("depends_on")):
                step = Step.from_yaml(compiled)
                step._from_devices = True
                step._origin = origin
                steps.append(step)
        else:
            check_written_step(raw)
            step = Step.from_yaml(raw)
            step._origin = origin
            steps.append(step)
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
            steps.extend(
                read_steps_from_file(yaml_path, global_config["github_repo_name"])
            )
    return steps


def read_steps_from_file(yaml_path: str, github_repo_name: str) -> List["Step"]:
    """One job file's steps, as the generator reads them."""
    with open(yaml_path, "r") as f:
        text = f.read()
    data = yaml.safe_load(text)
    source_file = os.path.relpath(yaml_path)
    if any(is_device_step(raw) for raw in data.get("steps") or []):
        check_block_style(text, source_file)
    return steps_from_document(data, source_file, github_repo_name)


def steps_from_document(
    data: dict, source_file: str, github_repo_name: str
) -> List["Step"]:
    """A parsed job file's steps, with what its file-level `depends_on` adds
    (the depends_on itself, the working-dir default and the file as a
    dependency)."""
    group_depends_on = data.get("depends_on")
    file_steps = parse_steps_from_yaml(data)
    for step in file_steps:
        step.source_file = source_file
        step.source_file_declared = source_file in (step.source_file_dependencies or [])
        exact = exact_mirror(step)
        if exact is not None:
            step.mirror_source_file_declared = source_file in (
                exact.get("source_file_dependencies") or []
            )
    if group_depends_on:
        for step in file_steps:
            # A `devices:` step's AMD job gets what its step gets here, except
            # depends_on, which an AMD job always sets.
            exact = exact_mirror(step)
            if exact is not None:
                if (
                    not exact.get("working_dir")
                    and github_repo_name == "vllm-project/vllm"
                ):
                    exact["working_dir"] = "/vllm-workspace/tests"
                exact["source_file_dependencies"] = list(
                    exact.get("source_file_dependencies") or []
                ) + [source_file]
            if not step.depends_on:
                step.depends_on = group_depends_on
            if not step.working_dir and github_repo_name == "vllm-project/vllm":
                step.working_dir = "/vllm-workspace/tests"
            step.source_file_dependencies = getattr(
                step, "source_file_dependencies", []
            )
            if not step.source_file_dependencies:
                step.source_file_dependencies = []
            step.source_file_dependencies.append(source_file)
    return file_steps


def check_device_step_keys(steps: List[Step]) -> None:
    """E8: fail on a key that a `devices:` step's job shares with any other
    job, in any file: Buildkite rejects the upload. Keys only legacy steps
    share are left alone, as before."""
    owners: Dict[str, List[Step]] = defaultdict(list)
    for step in steps:
        if step.key:
            owners[step.key].append(step)
        if step.key and (step.mirror or {}).get(MIRROR_HARDWARE):
            owners[f"amd-{step.key}"].append(step)
    for key, keyed in owners.items():
        if len(keyed) > 1 and any(step._from_devices for step in keyed):
            files = sorted({step.source_file or "?" for step in keyed})
            raise DeviceStepError(
                "E8",
                ", ".join(files),
                f"duplicate CI step key {key} (a `devices:` step's job); pin "
                "a different `key:` on it",
            )


def exact_mirror(step: Step) -> Optional[Dict[str, Any]]:
    """The step's `mirror.amd` when a `devices:` step compiled it: a whole
    AMD job, which inherits nothing from the step."""
    amd = (step.mirror or {}).get(MIRROR_HARDWARE)
    return amd if amd and amd.get(EXACT_MIRROR) else None


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
