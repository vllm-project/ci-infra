from pydantic import BaseModel
from typing import List, Dict
import os
import yaml

from utils import get_pr_labels, get_list_file_diff, should_run_all, should_use_precompiled, should_fail_fast
from step import read_steps_from_job_dir, group_steps
from buildkite_step import convert_group_step_to_buildkite_step
from global_config import init_global_config, get_global_config

class PipelineGenerator:
    def __init__(self, pipeline_config_path: str, output_file_path: str):
        init_global_config(pipeline_config_path)
        self.output_file_path = output_file_path

    def generate(self):
        global_config = get_global_config()
        self.pr_labels = get_pr_labels()
        self.list_file_diff = get_list_file_diff()
        self.run_all = should_run_all(self.pr_labels, self.list_file_diff)

        # vLLM only variables
        self.use_precompiled = should_use_precompiled(self.pr_labels, self.run_all)
        self.fail_fast = should_fail_fast(self.pr_labels)

        steps = []
        for job_dir in global_config["job_dirs"]:
            steps.extend(read_steps_from_job_dir(job_dir))
        grouped_steps = group_steps(steps)

        buildkite_group_steps = convert_group_step_to_buildkite_step(grouped_steps)
        buildkite_group_steps = sorted(buildkite_group_steps, key=lambda x: x.group)
        for group_step in buildkite_group_steps:
            for step in group_step.steps:
                if step.depends_on and "cpu" in step.depends_on:
                    print(step)
        buildkite_steps_dict = {"steps": []}
        for buildkite_group_step in buildkite_group_steps:
            buildkite_steps_dict["steps"].append(buildkite_group_step.dict(exclude_none=True))
        with open(self.output_file_path, "w") as f:
            yaml.dump(buildkite_steps_dict, f, sort_keys=False, default_flow_style=False)
        return buildkite_steps_dict
