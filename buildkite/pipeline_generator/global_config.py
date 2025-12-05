from typing import TypedDict, List, Dict, Optional
import yaml
import os
import subprocess
import re
import requests

class GlobalConfig(TypedDict):
    name: str
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

    config = GlobalConfig(
        name=pipeline_config["name"],
        job_dirs=pipeline_config["job_dirs"],
        registries=pipeline_config["registries"],
        repositories=pipeline_config["repositories"],
        branch=os.getenv("BUILDKITE_BRANCH"),
        commit=os.getenv("BUILDKITE_COMMIT"),
        pull_request=os.getenv("BUILDKITE_PULL_REQUEST"),
        docs_only_disable=os.getenv("DOCS_ONLY_DISABLE", "0"),
        run_all_patterns=pipeline_config.get("run_all_patterns", None),
        run_all_exclude_patterns=pipeline_config.get("run_all_exclude_patterns", None),
        nightly=os.getenv("NIGHTLY", "0"),
        run_all=_should_run_all(_get_pr_labels(os.getenv("BUILDKITE_PULL_REQUEST")), _get_list_file_diff(os.getenv("BUILDKITE_BRANCH"), _get_merge_base_commit()), pipeline_config.get("run_all_patterns", None), pipeline_config.get("run_all_exclude_patterns", None)),
        merge_base_commit=_get_merge_base_commit(),
        list_file_diff=_get_list_file_diff(os.getenv("BUILDKITE_BRANCH"), _get_merge_base_commit()),
        fail_fast=_should_fail_fast(_get_pr_labels(os.getenv("BUILDKITE_PULL_REQUEST"))),
    )

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

def _get_merge_base_commit() -> Optional[str]:
    """Get merge base commit from env var or compute it via git."""
    merge_base = os.getenv("MERGE_BASE_COMMIT")
    if merge_base:
        return merge_base
    # Compute merge base if not provided
    try:
        result = subprocess.run(
            ["git", "merge-base", "origin/main", "HEAD"],
            capture_output=True,
            text=True,
            check=True
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError:
        return None

def _should_run_all(pr_labels: List[str], list_file_diff: List[str], run_all_patterns: List[str], run_all_exclude_patterns: List[str]) -> bool:
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

def _get_list_file_diff(branch: str, merge_base_commit: Optional[str]) -> List[str]:
    """Get list of file paths that get changed between current branch and origin/main."""
    try:
        subprocess.run(["git", "add", "."], check=True)
        if branch == "main":
            output = subprocess.check_output(
                ["git", "diff", "--name-only", "--diff-filter=ACMDR", "HEAD~1"],
                universal_newlines=True
            )
        else:
            merge_base = merge_base_commit
            output = subprocess.check_output(
                ["git", "diff", "--name-only", "--diff-filter=ACMDR", merge_base.strip()],
                universal_newlines=True
            )
        return [line for line in output.split('\n') if line.strip()]
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Failed to get git diff: {e}")

def _get_pr_labels(pull_request: str) -> List[str]:
    if not pull_request or pull_request == "false":
        return []
    request_url = f"https://api.github.com/repos/vllm-project/vllm/pulls/{pull_request}"
    response = requests.get(request_url)
    response.raise_for_status()
    return [label["name"] for label in response.json()["labels"]]