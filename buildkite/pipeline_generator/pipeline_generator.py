import os
import subprocess
from typing import FrozenSet, List, Optional, Tuple

import yaml

from buildkite_step import (
    add_precommit_dependency,
    convert_group_step_to_buildkite_step,
    create_precommit_group_step,
)
from global_config import get_global_config, init_global_config
from step import Step, group_steps, read_steps_from_job_dir


class PipelineGenerator:
    def __init__(
        self,
        pipeline_config_path: str,
        output_file_path: str,
        docs_only_disable: bool = False,
    ):
        init_global_config(pipeline_config_path)
        self.output_file_path = output_file_path

    def generate(self):
        global_config = get_global_config()

        # Skip if changes are doc-only (unless RUN_ALL is set)
        if (
            global_config["docs_only_disable"] == "0"
            and not global_config["run_all"]
            and global_config["only_step_keys"] is None
        ):
            if is_docs_only_change(global_config["list_file_diff"]):
                print("List file diff: ", global_config["list_file_diff"])
                print("All changes are doc-only, skipping CI.")
                subprocess.run(
                    [
                        "buildkite-agent",
                        "annotate",
                        ":memo: CI skipped — doc-only changes",
                    ],
                    check=True,
                )
                output_dir_path = os.path.dirname(self.output_file_path)
                with open(os.path.join(output_dir_path, ".docs_only"), "w") as f:
                    f.write("true")
                return

        steps = []
        for job_dir in global_config["job_dirs"]:
            steps.extend(read_steps_from_job_dir(job_dir))
        steps, selected_step_keys = select_steps_and_dependencies(
            steps, global_config["only_step_keys"]
        )
        global_config["only_step_keys"] = selected_step_keys
        grouped_steps = group_steps(steps)

        buildkite_group_steps = convert_group_step_to_buildkite_step(grouped_steps)
        buildkite_group_steps = sorted(buildkite_group_steps, key=lambda x: x.group)

        # Run pre-commit as a dedicated step in parallel with the image build.
        # Steps that depend on the image build also wait for pre-commit to pass.
        # Place it first so it shows up right after the bootstrap step.
        if global_config["pull_request"] and global_config["pull_request"] != "false":
            add_precommit_dependency(buildkite_group_steps)
            buildkite_group_steps.insert(
                0,
                create_precommit_group_step(
                    global_config["github_repo_name"], global_config["commit"]
                ),
            )

        buildkite_steps_dict = {"steps": []}
        for buildkite_group_step in buildkite_group_steps:
            buildkite_steps_dict["steps"].append(
                buildkite_group_step.dict(exclude_none=True)
            )
        with open(self.output_file_path, "w") as f:
            yaml.dump(
                buildkite_steps_dict, f, sort_keys=False, default_flow_style=False
            )
        return


def select_steps_and_dependencies(
    steps: List[Step],
    requested_step_keys: Optional[FrozenSet[str]],
) -> Tuple[List[Step], Optional[FrozenSet[str]]]:
    if requested_step_keys is None:
        return steps, None

    steps_by_key = {}
    for step in steps:
        if not step.key:
            continue
        if step.key in steps_by_key:
            raise ValueError(f"Duplicate CI step key: {step.key}")
        steps_by_key[step.key] = step

    missing = requested_step_keys - steps_by_key.keys()
    if missing:
        raise ValueError("Unknown CI step key(s): " + ", ".join(sorted(missing)))

    selected_step_keys = set(requested_step_keys)
    pending = list(requested_step_keys)
    while pending:
        step_key = pending.pop()
        for dependency in steps_by_key[step_key].depends_on or []:
            if dependency not in steps_by_key:
                raise ValueError(
                    f"CI step {step_key} depends on unknown step {dependency}."
                )
            if dependency not in selected_step_keys:
                selected_step_keys.add(dependency)
                pending.append(dependency)

    selected = [step for step in steps if step.key in selected_step_keys]
    return selected, frozenset(selected_step_keys)


def is_docs_only_change(list_file_diff: List[str]) -> bool:
    if len(list_file_diff) == 0:
        return False
    for file_path in list_file_diff:
        if not file_path:
            continue
        if file_path.startswith("docs/"):
            continue
        if file_path.endswith(".md"):
            continue
        if file_path == "mkdocs.yaml":
            continue
        return False
    return True
