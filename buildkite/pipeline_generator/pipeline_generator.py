import json
import os
import shutil
import subprocess
from typing import Dict, FrozenSet, List, Optional, Tuple

import yaml

from amd import is_amd_device, normalize_amd_depends_on
from buildkite_step import (
    AMD_GROUP,
    COLLECT_GROUP,
    PRECOMMIT_GROUP,
    BuildkiteGroupStep,
    _generate_step_key,
    add_precommit_dependency,
    convert_group_step_to_buildkite_step,
    fnrec_collect_group,
    kernrec_collect_group,
    kernrec_enabled,
    create_precommit_group_step,
    split_deferred_steps,
)
from constants import AgentQueue
from recorder_switches import fnrec_enabled
from global_config import get_global_config, init_global_config
from queue_fallbacks import apply_queue_fallbacks
from step import Step, group_steps, read_steps_from_job_dir
import unblock_jobs


def _annotate_best_effort(message: str, style: Optional[str] = None) -> None:
    """Best-effort `buildkite-agent annotate`: never fail the build over it.

    check=False only covers a non-zero exit; it still lets FileNotFoundError
    propagate if the binary itself is missing (e.g. when generating locally).
    """
    command = ["buildkite-agent", "annotate", message]
    if style:
        command.extend(["--style", style])
    try:
        subprocess.run(command, check=False)
    except OSError:
        pass


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

        torch_nightly = global_config["torch_nightly"] == "1"
        if torch_nightly:
            # ROCm has its own pinned torch and gains nothing from validating
            # against a CUDA torch-nightly build, so the AMD lane is excluded
            # further downstream (see is_amd_device / include_amd).
            _annotate_best_effort(
                "AMD lane excluded: torch-nightly validates CUDA only."
            )

        steps = []
        for job_dir in global_config["job_dirs"]:
            steps.extend(read_steps_from_job_dir(job_dir))
        try:
            steps, selected_step_keys = select_steps_and_dependencies(
                steps, global_config["only_step_keys"], torch_nightly=torch_nightly
            )
        except ValueError as error:
            # Surface the reason on the build page, not only in the bootstrap log.
            _annotate_best_effort(str(error), style="error")
            raise
        global_config["only_step_keys"] = selected_step_keys
        steps, probes = apply_queue_fallbacks(steps, global_config)
        grouped_steps = group_steps(steps)

        buildkite_group_steps = convert_group_step_to_buildkite_step(grouped_steps)
        buildkite_group_steps = (
            sorted(buildkite_group_steps, key=lambda x: x.group) + probes
        )
        group_of_step = {
            step.key: group_step.group
            for group_step in buildkite_group_steps
            for step in group_step.steps
        }
        buildkite_group_steps, deferred_group_steps = split_deferred_steps(
            buildkite_group_steps
        )

        # A recording build ends by collecting every job's recordings.
        # Both recorders share one group, so they don't make two groups
        # with the same name.
        collect = []
        if kernrec_enabled():
            collect += kernrec_collect_group(buildkite_group_steps).steps
        if fnrec_enabled():
            collect += fnrec_collect_group(buildkite_group_steps).steps
        if collect:
            buildkite_group_steps.append(
                BuildkiteGroupStep(group=COLLECT_GROUP, steps=collect)
            )

        # Run pre-commit as a dedicated step in parallel with the image build.
        # Steps that depend on the image build also wait for pre-commit to pass.
        # Place it first so it shows up right after the bootstrap step.
        if global_config["pull_request"] and global_config["pull_request"] != "false":
            add_precommit_dependency(buildkite_group_steps)
            add_precommit_dependency(deferred_group_steps)
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
        if deferred_group_steps:
            # Right after pre-commit (if any), near the top of the build page.
            position = int(
                bool(buildkite_group_steps)
                and buildkite_group_steps[0].group == PRECOMMIT_GROUP
            )
            buildkite_steps_dict["steps"].insert(
                position,
                write_deferred_steps(
                    deferred_group_steps,
                    os.path.dirname(self.output_file_path),
                    group_of_step,
                ),
            )
        with open(self.output_file_path, "w") as f:
            yaml.dump(
                buildkite_steps_dict, f, sort_keys=False, default_flow_style=False
            )
        return


def write_deferred_steps(
    deferred_group_steps: List[BuildkiteGroupStep],
    output_dir: str,
    group_of_step: Optional[Dict[str, str]] = None,
) -> dict:
    """Save the steps left out of the pipeline for the "Run more jobs" input
    step, and return its group.

    The input step shows one checkbox list per group. AMD steps all share one
    group of a few hundred, so each is listed under its NVIDIA step's group
    instead ("AMD - Kernels"), after the NVIDIA lists. `group_of_step` maps
    every generated step key to its group, to find that.

    They go up as artifacts of this job, with the script that uploads the
    picked ones, so that job runs exactly what was generated here.
    """
    artifact_dir = os.path.relpath(os.path.join(output_dir, unblock_jobs.DEFERRED_DIR))
    os.makedirs(artifact_dir, exist_ok=True)
    source_job = os.getenv("BUILDKITE_JOB_ID") or "bootstrap"
    queue = AgentQueue.SMALL_CPU_PREMERGE.value
    group_of_step = group_of_step or {}
    entries = []
    amd_entries = []
    for group_step in deferred_group_steps:
        for step in group_step.steps:
            entry = {"group": group_step.group, "step": step.dict(exclude_none=True)}
            if group_step.group == AMD_GROUP:
                parent = step.key[len("amd-") :] if step.key.startswith("amd-") else ""
                entry["list"] = f"AMD - {group_of_step.get(parent, 'Other')}"
                amd_entries.append(entry)
            else:
                entries.append(entry)
    entries += sorted(amd_entries, key=lambda entry: entry["list"])
    with open(os.path.join(artifact_dir, unblock_jobs.DEFERRED_FILE), "w") as f:
        json.dump({"source_job": source_job, "queue": queue, "steps": entries}, f)
    shutil.copyfile(
        unblock_jobs.__file__, os.path.join(artifact_dir, unblock_jobs.SCRIPT_FILE)
    )
    if os.getenv("BUILDKITE_JOB_ID"):
        # Without these the input step has nothing to upload, so fail loudly.
        subprocess.run(
            ["buildkite-agent", "artifact", "upload", f"{artifact_dir}/*"],
            check=True,
        )
    return unblock_jobs.more_jobs_group(entries, 1, artifact_dir, source_job, queue)


def select_steps_and_dependencies(
    steps: List[Step],
    requested_step_keys: Optional[FrozenSet[str]],
    torch_nightly: bool = False,
) -> Tuple[List[Step], Optional[FrozenSet[str]]]:
    if requested_step_keys is None:
        return steps, None

    steps_by_key = {}
    dependencies_by_key = {}
    amd_step_keys = set()
    for step in steps:
        # Steps without an explicit key are uploaded with a label-derived key
        # (see convert_group_step_to_buildkite_step), so retry builds reference
        # them by that generated key.
        if not step.key:
            step.key = _generate_step_key(step.label)
        if step.key in steps_by_key:
            raise ValueError(f"Duplicate CI step key: {step.key}")
        steps_by_key[step.key] = step
        dependencies_by_key[step.key] = step.depends_on or []
        if is_amd_device(step.device):
            amd_step_keys.add(step.key)

        # AMD mirrors are uploaded as generated `amd-<key>` steps, so retry
        # builds reference them by that key too.
        amd_mirror = (step.mirror or {}).get("amd")
        if amd_mirror:
            mirror_key = f"amd-{step.key}"
            if mirror_key in steps_by_key:
                raise ValueError(f"Duplicate CI step key: {mirror_key}")
            steps_by_key[mirror_key] = step
            dependencies_by_key[mirror_key] = normalize_amd_depends_on(
                amd_mirror.get("depends_on")
            )
            amd_step_keys.add(mirror_key)

    missing = requested_step_keys - steps_by_key.keys()
    if missing:
        raise ValueError("Unknown CI step key(s): " + ", ".join(sorted(missing)))

    if torch_nightly:
        requested_amd_keys = requested_step_keys & amd_step_keys
        if requested_amd_keys:
            raise ValueError(
                "AMD CI step key(s) requested, but AMD steps are excluded from "
                "torch-nightly runs: " + ", ".join(sorted(requested_amd_keys))
            )

    selected_step_keys = set(requested_step_keys)
    pending = list(requested_step_keys)
    while pending:
        step_key = pending.pop()
        for dependency in dependencies_by_key[step_key]:
            if dependency not in steps_by_key:
                raise ValueError(
                    f"CI step {step_key} depends on unknown step {dependency}."
                )
            if dependency not in selected_step_keys:
                selected_step_keys.add(dependency)
                pending.append(dependency)

    selected = [
        step
        for step in steps
        if step.key in selected_step_keys or f"amd-{step.key}" in selected_step_keys
    ]
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
