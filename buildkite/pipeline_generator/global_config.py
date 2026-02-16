from typing import TypedDict, List, Dict, Optional
import yaml
import os
import re
from utils_lib.git_utils import get_merge_base_commit, get_list_file_diff, get_pr_labels


class GlobalConfig(TypedDict):
    name: str
    github_repo_name: str
    job_dirs: List[str]
    registries: str
    repositories: Dict[str, str]
    branch: str
    commit: str
    pull_request: Optional[str] = None
    run_all_patterns: Optional[List[str]] = None
    run_all_exclude_patterns: Optional[List[str]] = None
    nightly: Optional[str] = "0"
    run_all: bool = False
    docs_only_disable: Optional[str] = "0"
    merge_base_commit: Optional[str] = None
    fail_fast: bool = False


config = None


def init_global_config(pipeline_config_path: str):
    global config
    if config:
        return
    pipeline_config = yaml.safe_load(open(pipeline_config_path, "r"))
    _validate_pipeline_config(pipeline_config)

    if "github_repo_name" not in pipeline_config:
        pipeline_config["github_repo_name"] = "vllm-project/vllm"

    branch = os.getenv("BUILDKITE_BRANCH")
    pull_request = os.getenv("BUILDKITE_PULL_REQUEST")
    merge_base_commit = get_merge_base_commit()
    list_file_diff = get_list_file_diff(branch, merge_base_commit)
    pr_labels = get_pr_labels(pull_request, pipeline_config["github_repo_name"])

    config = GlobalConfig(
        name=pipeline_config["name"],
        github_repo_name=pipeline_config["github_repo_name"],
        job_dirs=pipeline_config["job_dirs"],
        registries=pipeline_config["registries"],
        repositories=pipeline_config["repositories"],
        branch=branch,
        commit=os.getenv("BUILDKITE_COMMIT"),
        pull_request=pull_request,
        docs_only_disable=os.getenv("DOCS_ONLY_DISABLE", "0"),
        run_all_patterns=pipeline_config.get("run_all_patterns", None),
        run_all_exclude_patterns=pipeline_config.get("run_all_exclude_patterns", None),
        nightly=os.getenv("NIGHTLY", "0"),
        run_all=_should_run_all(
            pr_labels,
            list_file_diff,
            pipeline_config.get("run_all_patterns", None),
            pipeline_config.get("run_all_exclude_patterns", None),
        ),
        merge_base_commit=merge_base_commit,
        list_file_diff=list_file_diff,
        fail_fast=_should_fail_fast(pr_labels),
    )
    if "ready-run-all-tests" in pr_labels:
        config["run_all"] = True
        config["nightly"] = "1"
    print("Config:\n")
    for key, value in config.items():
        print(f"{key}: {value}\n")


def get_global_config():
    global config
    if not config:
        raise ValueError("Global config not initialized")
    return config


def _validate_pipeline_config(pipeline_config: Dict):
    if not pipeline_config["name"]:
        raise ValueError("Pipeline name is required")
    if not pipeline_config["job_dirs"]:
        raise ValueError("Job directories are required")
    if not pipeline_config["registries"]:
        raise ValueError("Registries are required")
    if not pipeline_config["repositories"]:
        raise ValueError("Repositories are required")
    for job_dir in pipeline_config["job_dirs"]:
        if not os.path.exists(job_dir):
            raise ValueError(f"Job directory not found: {job_dir}")


def _should_run_all(
    pr_labels: List[str],
    list_file_diff: List[str],
    run_all_patterns: List[str],
    run_all_exclude_patterns: List[str],
) -> bool:
    """Determine if the pipeline should run all tests."""
    if os.getenv("RUN_ALL") == "1":
        return True
    if "ready-run-all-tests" in pr_labels:
        return True
    pattern_matched = False
    for file in list_file_diff:
        for pattern in run_all_patterns:
            if re.match(pattern, file):
                pattern_matched = True
                break
        if pattern_matched:
            match_ignore = False
            for exclude_pattern in run_all_exclude_patterns:
                if re.match(exclude_pattern, file):
                    match_ignore = True
                    break
            if not match_ignore:
                return True
    return False


def _should_fail_fast(pr_labels: List[str]) -> bool:
    if "ci-no-fail-fast" in pr_labels:
        return False
    return True
