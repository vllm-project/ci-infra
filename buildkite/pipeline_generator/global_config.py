import json
import os
import re
from typing import Dict, FrozenSet, List, Optional, TypedDict

import yaml

from utils_lib.git_utils import get_merge_base_commit, get_list_file_diff, get_pr_labels


ONLY_STEP_KEYS_ENV_VAR = "VLLM_CI_ONLY_STEP_KEYS"
STEP_KEY_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")


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
    torch_nightly: Optional[str] = "0"
    run_all: bool = False
    docs_only_disable: Optional[str] = "0"
    merge_base_commit: Optional[str] = None
    fail_fast: bool = False
    amd_hf_offline_retry: bool = False
    disable_hf_offline_retry: bool = False
    only_step_keys: Optional[FrozenSet[str]] = None


config = None
HF_OFFLINE_RETRY_CAPABILITY_KEY = "amd_hf_offline_retry"
HF_OFFLINE_RETRY_KILL_SWITCH_ENV = "VLLM_CI_DISABLE_HF_OFFLINE_RETRY"


def _read_strict_bool_env(name: str) -> bool:
    value = os.getenv(name)
    if value is None:
        return False
    if value not in {"0", "1"}:
        raise ValueError(f"{name} must be exactly '0' or '1', got {value!r}.")
    return value == "1"


def init_global_config(pipeline_config_path: str):
    global config
    if config:
        return
    pipeline_config = yaml.safe_load(open(pipeline_config_path, "r"))
    _validate_pipeline_config(pipeline_config)
    disable_hf_offline_retry = _read_strict_bool_env(HF_OFFLINE_RETRY_KILL_SWITCH_ENV)

    if "github_repo_name" not in pipeline_config:
        pipeline_config["github_repo_name"] = "vllm-project/vllm"

    branch = os.getenv("BUILDKITE_BRANCH")
    if branch:
        # Fork PRs arrive as "owner:branch" (e.g. "octocat:my-feature"), so the
        # colon must be allowed. It is not a shell metacharacter, so permitting
        # it does not reintroduce command-injection risk.
        if not re.match(r"^[a-zA-Z0-9._/:-]+$", branch):
            raise ValueError(
                f"Invalid branch name: {branch}. Contains disallowed characters."
            )
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
        torch_nightly=os.getenv("TORCH_NIGHTLY", "0"),
        run_all=_should_run_all(
            pr_labels,
            list_file_diff,
            pipeline_config.get("run_all_patterns", None),
            pipeline_config.get("run_all_exclude_patterns", None),
        ),
        merge_base_commit=merge_base_commit,
        list_file_diff=list_file_diff,
        fail_fast=_should_fail_fast(pr_labels),
        amd_hf_offline_retry=pipeline_config.get(
            HF_OFFLINE_RETRY_CAPABILITY_KEY, False
        ),
        disable_hf_offline_retry=disable_hf_offline_retry,
        only_step_keys=_parse_only_step_keys(os.getenv(ONLY_STEP_KEYS_ENV_VAR)),
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


def _parse_only_step_keys(value: Optional[str]) -> Optional[FrozenSet[str]]:
    if value is None:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as error:
        raise ValueError(
            f"{ONLY_STEP_KEYS_ENV_VAR} must be a JSON array of step keys."
        ) from error
    if not isinstance(parsed, list) or not parsed:
        raise ValueError(f"{ONLY_STEP_KEYS_ENV_VAR} must be a non-empty JSON array.")
    if any(
        not isinstance(key, str) or not STEP_KEY_PATTERN.fullmatch(key)
        for key in parsed
    ):
        raise ValueError(f"{ONLY_STEP_KEYS_ENV_VAR} contains an invalid step key.")
    if len(set(parsed)) != len(parsed):
        raise ValueError(f"{ONLY_STEP_KEYS_ENV_VAR} contains duplicate step keys.")
    return frozenset(parsed)


def _validate_pipeline_config(pipeline_config: Dict):
    if not pipeline_config["name"]:
        raise ValueError("Pipeline name is required")
    if not pipeline_config["job_dirs"]:
        raise ValueError("Job directories are required")
    if not pipeline_config["registries"]:
        raise ValueError("Registries are required")
    if not pipeline_config["repositories"]:
        raise ValueError("Repositories are required")
    amd_hf_offline_retry = pipeline_config.get(HF_OFFLINE_RETRY_CAPABILITY_KEY, False)
    if type(amd_hf_offline_retry) is not bool:
        raise ValueError(f"{HF_OFFLINE_RETRY_CAPABILITY_KEY} must be a boolean.")
    if "github_repo_name" in pipeline_config:
        repo_name = pipeline_config["github_repo_name"]
        if not re.match(r"^vllm-project/[a-zA-Z0-9._-]+$", repo_name):
            raise ValueError(
                f"Invalid github_repo_name: {repo_name}. Must be in format vllm-project/repo_name"
            )
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
    if os.getenv("TORCH_NIGHTLY") == "1":
        # A full torch-nightly run also runs the full suite on the pinned torch.
        return True
    if "ready-run-all-tests" in pr_labels:
        return True
    for file in list_file_diff:
        pattern_matched = False
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
