import json
import os
import re
from typing import Dict, FrozenSet, List, Optional, TypedDict

import yaml
from utils_lib.git_utils import get_list_file_diff, get_merge_base_commit, get_pr_labels

ONLY_STEP_KEYS_ENV_VAR = "VLLM_CI_ONLY_STEP_KEYS"
TRACE_CANARY_BRANCH_ENV_VAR = "VLLM_CI_TRACE_CANARY_BRANCH"
TRACE_CANARY_COMMIT_ENV_VAR = "VLLM_CI_TRACE_CANARY_COMMIT"
TRACE_S3_BUCKET_ENV_VAR = "VLLM_CI_TRACE_S3_BUCKET"
TRACE_S3_PREFIX_ENV_VAR = "VLLM_CI_TRACE_S3_PREFIX"
TRACE_IMAGE_DIGEST_ENV_VAR = "VLLM_CI_TRACE_IMAGE_DIGEST"
DEFAULT_VLLM_TRACE_BUCKET = "vllm-ci-test-selection-traces-936637512419-us-east-1"
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
    only_step_keys: Optional[FrozenSet[str]] = None
    trace_canary_branch: Optional[str] = None
    trace_canary_commit: Optional[str] = None
    trace_s3_bucket: Optional[str] = None
    trace_s3_prefix: str = "test-selection/vllm"
    trace_image_digest: Optional[str] = None


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

    only_step_keys = _parse_only_step_keys(os.getenv(ONLY_STEP_KEYS_ENV_VAR))
    nightly = os.getenv("NIGHTLY", "0")
    commit = os.getenv("BUILDKITE_COMMIT")
    trace_canary_branch, trace_canary_commit = _parse_trace_canary_override(
        branch,
        commit,
        os.getenv(TRACE_CANARY_BRANCH_ENV_VAR),
        os.getenv(TRACE_CANARY_COMMIT_ENV_VAR),
    )
    trace_s3_bucket = os.getenv(TRACE_S3_BUCKET_ENV_VAR)
    if (
        pipeline_config["github_repo_name"] == "vllm-project/vllm"
        and nightly == "1"
        and branch == "main"
        and pull_request in (None, "false")
    ):
        trace_s3_bucket = trace_s3_bucket or DEFAULT_VLLM_TRACE_BUCKET
    if trace_s3_bucket and not re.fullmatch(
        r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", trace_s3_bucket
    ):
        raise ValueError(f"{TRACE_S3_BUCKET_ENV_VAR} is invalid.")
    trace_s3_prefix = os.getenv(TRACE_S3_PREFIX_ENV_VAR, "test-selection/vllm")
    if not re.fullmatch(r"[a-zA-Z0-9._/-]+", trace_s3_prefix) or any(
        part in ("", ".", "..") for part in trace_s3_prefix.split("/")
    ):
        raise ValueError(f"{TRACE_S3_PREFIX_ENV_VAR} is invalid.")
    if trace_canary_branch:
        if (
            pipeline_config["github_repo_name"] != "vllm-project/vllm"
            or nightly != "1"
            or pull_request not in (None, "false")
            or not trace_s3_bucket
        ):
            raise ValueError(
                "trace canary override requires a trusted vLLM non-PR nightly "
                "with an explicit trace bucket"
            )
        if trace_s3_prefix == "test-selection/vllm":
            raise ValueError("trace canary override requires an isolated S3 prefix")
    trace_image_digest = os.getenv(TRACE_IMAGE_DIGEST_ENV_VAR)
    if trace_image_digest:
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", trace_image_digest):
            raise ValueError(
                f"{TRACE_IMAGE_DIGEST_ENV_VAR} must be sha256:<64 lowercase hex>"
            )
        if trace_canary_branch is None:
            raise ValueError(
                f"{TRACE_IMAGE_DIGEST_ENV_VAR} requires the trace canary "
                "trust tuple; refusing a mutable-tag build to claim pinned "
                "evidence"
            )
    config = GlobalConfig(
        name=pipeline_config["name"],
        github_repo_name=pipeline_config["github_repo_name"],
        job_dirs=pipeline_config["job_dirs"],
        registries=pipeline_config["registries"],
        repositories=pipeline_config["repositories"],
        branch=branch,
        commit=commit,
        pull_request=pull_request,
        docs_only_disable=os.getenv("DOCS_ONLY_DISABLE", "0"),
        run_all_patterns=pipeline_config.get("run_all_patterns", None),
        run_all_exclude_patterns=pipeline_config.get("run_all_exclude_patterns", None),
        nightly=nightly,
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
        only_step_keys=only_step_keys,
        trace_canary_branch=trace_canary_branch,
        trace_canary_commit=trace_canary_commit,
        trace_s3_bucket=trace_s3_bucket,
        trace_s3_prefix=trace_s3_prefix,
        trace_image_digest=trace_image_digest,
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
    return _parse_step_keys(value, ONLY_STEP_KEYS_ENV_VAR)


def _parse_trace_canary_override(
    branch: Optional[str],
    commit: Optional[str],
    allowed_branch: Optional[str],
    allowed_commit: Optional[str],
) -> tuple[Optional[str], Optional[str]]:
    if (allowed_branch is None) != (allowed_commit is None):
        raise ValueError(
            f"{TRACE_CANARY_BRANCH_ENV_VAR} and {TRACE_CANARY_COMMIT_ENV_VAR} "
            "must be set together."
        )
    if allowed_branch is None:
        return None, None
    if branch == "main":
        raise ValueError("trace canary overrides are forbidden on main")
    if allowed_branch != branch:
        raise ValueError(
            f"{TRACE_CANARY_BRANCH_ENV_VAR} must exactly match BUILDKITE_BRANCH."
        )
    if not re.fullmatch(r"[0-9a-f]{40}", allowed_commit or ""):
        raise ValueError(f"{TRACE_CANARY_COMMIT_ENV_VAR} must be one 40-hex SHA.")
    if allowed_commit != commit:
        raise ValueError(
            f"{TRACE_CANARY_COMMIT_ENV_VAR} must exactly match BUILDKITE_COMMIT."
        )
    return allowed_branch, allowed_commit


def _parse_step_keys(
    value: Optional[str], environment_variable: str
) -> Optional[FrozenSet[str]]:
    if value is None:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as error:
        raise ValueError(
            f"{environment_variable} must be a JSON array of step keys."
        ) from error
    if not isinstance(parsed, list) or not parsed:
        raise ValueError(f"{environment_variable} must be a non-empty JSON array.")
    if any(
        not isinstance(key, str) or not STEP_KEY_PATTERN.fullmatch(key)
        for key in parsed
    ):
        raise ValueError(f"{environment_variable} contains an invalid step key.")
    if len(set(parsed)) != len(parsed):
        raise ValueError(f"{environment_variable} contains duplicate step keys.")
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
