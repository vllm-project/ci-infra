from pydantic import BaseModel
from typing import List, Dict
import os
import yaml
import subprocess
import sys
from utils import is_docs_only_change
from step import read_steps_from_job_dir, group_steps
from buildkite_step import convert_group_step_to_buildkite_step
from global_config import init_global_config, get_global_config

class PipelineGenerator:
    def __init__(self, pipeline_config_path: str, output_file_path: str, docs_only_disable: bool = False):
        init_global_config(pipeline_config_path)
        self.output_file_path = output_file_path

    def generate(self):
        global_config = get_global_config()

        # Skip if changes are doc-only
        if global_config["docs_only_disable"] == "0":
            if is_docs_only_change(global_config["list_file_diff"]):
                print("List file diff: ", global_config["list_file_diff"])
                print("All changes are doc-only, skipping CI.")
                subprocess.run([
                    "buildkite-agent",
                    "annotate",
                    ":memo: CI skipped — doc-only changes"
                    ],
                    check=True
                )
            sys.exit(0)

        image_build_steps = get_image_build_steps()
        steps = []
        for job_dir in global_config["job_dirs"]:
            steps.extend(read_steps_from_job_dir(job_dir))
        grouped_steps = group_steps(steps)

        buildkite_group_steps = convert_group_step_to_buildkite_step(grouped_steps)
        buildkite_group_steps = sorted(buildkite_group_steps, key=lambda x: x.group)
        buildkite_steps_dict = {"steps": []}
        for buildkite_group_step in buildkite_group_steps:
            buildkite_steps_dict["steps"].append(buildkite_group_step.dict(exclude_none=True))
        with open(self.output_file_path, "w") as f:
            yaml.dump(buildkite_steps_dict, f, sort_keys=False, default_flow_style=False)
        return buildkite_steps_dict
