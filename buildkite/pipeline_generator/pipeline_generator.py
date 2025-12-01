from pydantic import BaseModel
from typing import List, Dict
import os
import yaml

from pipeline_generator_helper import get_pr_labels, get_list_file_diff, should_run_all, should_use_precompiled, should_fail_fast
from step import read_steps_from_job_dir, group_and_sort_steps
from buildkite_step import convert_group_step_to_buildkite_step
from pipeline_generator_helper import get_image

class PipelineConfig(BaseModel):
    name: str
    job_dirs: List[str]
    run_all_patterns: List[str]
    run_all_exclude_patterns: List[str]
    registries: Dict[str, str]

    def __init__(self, name: str, job_dirs: List[str], run_all_patterns: List[str], run_all_exclude_patterns: List[str], registries: Dict[str, str]):
        super().__init__(name=name, job_dirs=job_dirs, run_all_patterns=run_all_patterns, run_all_exclude_patterns=run_all_exclude_patterns, registries=registries)

    @classmethod
    def from_yaml(cls, yaml_path: str):
        if not os.path.exists(yaml_path):
            raise FileNotFoundError(f"Pipeline config file not found: {yaml_path}")
        with open(yaml_path, "r") as f:
            data = yaml.safe_load(f)
            print(data)
        return cls(name=data["name"], job_dirs=data["job_dirs"], run_all_patterns=data["run_all_patterns"], run_all_exclude_patterns=data["run_all_exclude_patterns"], registries=data["registries"])
    
    def validate(self):
        for job_dir in self.job_dirs:
            if not os.path.exists(job_dir):
                raise FileNotFoundError(f"Job directory not found: {job_dir}")


class PipelineGenerator:
    def __init__(self, pipeline_config_path: str, output_file_path: str):
        self.branch = os.getenv("BUILDKITE_BRANCH")
        self.pull_request = os.getenv("BUILDKITE_PULL_REQUEST")
        self.commit = os.getenv("BUILDKITE_COMMIT")
        self.pipeline_config = PipelineConfig.from_yaml(pipeline_config_path)
        self.pipeline_config.validate()
        self.output_file_path = output_file_path

    def generate(self):
        self.pr_labels = get_pr_labels(self.pull_request)
        self.list_file_diff = get_list_file_diff(self.branch)
        self.run_all = should_run_all(self.pr_labels, self.list_file_diff, self.pipeline_config.run_all_patterns, self.pipeline_config.run_all_exclude_patterns)

        # vLLM only variables
        self.use_precompiled = should_use_precompiled(self.pr_labels, self.run_all)
        self.fail_fast = should_fail_fast(self.pr_labels)
    
        steps = []
        for job_dir in self.pipeline_config.job_dirs:
            steps.extend(read_steps_from_job_dir(job_dir))
        group_steps = group_and_sort_steps(steps)
        image = get_image(self.pipeline_config.registries, self.branch, self.commit)
        variables_to_inject = {
            "$REPO": self.pipeline_config.registries["main"] if self.branch == "main" else self.pipeline_config.registries["premerge"],
            "$BUILDKITE_COMMIT": self.commit,
        }
        buildkite_group_steps = convert_group_step_to_buildkite_step(group_steps, image, variables_to_inject)
        buildkite_group_steps = sorted(buildkite_group_steps, key=lambda x: x.group)
        for buildkite_group_step in buildkite_group_steps:
            print(buildkite_group_step.group)
        buildkite_steps_dict = {"steps": []}
        for buildkite_group_step in buildkite_group_steps:
            buildkite_steps_dict["steps"].append(buildkite_group_step.dict(exclude_none=True))
        with open("buildkite_steps.yaml", "w") as f:
            yaml.dump(buildkite_steps_dict, f, sort_keys=False, default_flow_style=False)
        with open(self.output_file_path, "w") as f:
            f.write("debug null")
        with open(self.output_file_path, "r") as f:
            print(f.read())
        # print(self.output_file_path)
        # with open(self.output_file_path, "w") as f:
        #     yaml.dump(buildkite_steps_dict, f, sort_keys=False, default_flow_style=False)
